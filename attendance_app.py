import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import altair as alt
import re
import io

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

st.set_page_config(
    page_title="VTU Attendance System", 
    page_icon="🎓", 
    layout="wide"
)

# Session State Initialization
if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# Initialize Firebase (Crash-Proof Strategy)
if not firebase_admin._apps:
    try:
        # Strategy 1: Streamlit Cloud Secrets
        if "firebase" in st.secrets:
            key_dict = dict(st.secrets["firebase"])
            cred = credentials.Certificate(key_dict)
        # Strategy 2: Local File
        else:
            cred = credentials.Certificate("firebase_key.json")
        firebase_admin.initialize_app(cred)
    except Exception as e:
        # If app is already initialized, we pass.
        pass

db = firestore.client()

# ==========================================
# 2. CACHING & OPTIMIZATION
# ==========================================

@st.cache_data(ttl=3600)
def get_students_cached(dept, sem, section):
    """
    Fetches student list from DB. 
    Cached for 1 hour to reduce Firestore reads.
    """
    # Normalize inputs to match DB storage format
    c_dept = str(dept).strip().upper()
    c_sem = str(sem).strip()
    c_sec = str(section).strip().upper()
    
    docs = db.collection('Students')\
        .where("dept", "==", c_dept)\
        .where("sem", "==", c_sem)\
        .where("section", "==", c_sec).stream()
    
    return [{"usn": d.id, **d.to_dict()} for d in docs]

@st.cache_data(ttl=600)
def get_faculty_courses(faculty_id):
    """
    Fetches courses assigned to the logged-in faculty.
    Cached for 10 minutes.
    """
    docs = db.collection('Courses').where("faculty_id", "==", faculty_id).stream()
    return [d.to_dict() for d in docs]

# ==========================================
# 3. DATA HELPERS
# ==========================================

def sanitize_key(val):
    """
    Cleans USN or SubCode to be used as a Dictionary Key or Doc ID.
    Removes dots, slashes, and spaces to prevent Firestore errors.
    """
    if not val: return ""
    return str(val).strip().upper().replace(".", "_").replace("/", "_").replace(" ", "")

def generate_email(name, existing_email=None):
    """
    Generates a dummy email if one is not provided in CSV.
    Format: firstname.lastname@amc.edu
    """
    val = str(existing_email).strip().lower()
    if val and val not in ['nan', 'none', '']:
        return val
    # Clean name: Keep only alphanumeric, replace rest with dot
    clean_name = re.sub(r'[^a-zA-Z0-9]', '.', str(name).strip().lower())
    # Remove repeated dots
    clean_name = re.sub(r'\.+', '.', clean_name).strip('.')
    return f"{clean_name}@amc.edu"

# ==========================================
# 4. REPORT GENERATORS
# ==========================================

def generate_session_report(dept, start_date, end_date):
    """
    1. Class Log Report: Shows date-wise classes conducted.
    Useful for auditing faculty performance.
    """
    # Step A: Fetch Course Info to map SubCode -> Sem/Title
    all_courses = db.collection('Courses').stream()
    course_lookup = {}
    for c in all_courses:
        d = c.to_dict()
        if d.get('dept') == dept:
            course_lookup[d['subcode']] = {
                'sem': d.get('sem', 'N/A'),
                'title': d.get('subtitle', '')
            }

    # Step B: Query Logs by Date Range
    sessions = db.collection('Class_Sessions')\
        .where("date", ">=", str(start_date))\
        .where("date", "<=", str(end_date))\
        .stream()
        
    data = []
    for s in sessions:
        d = s.to_dict()
        subcode = d.get('course_code', '')
        
        # Step C: Filter by Dept via SubCode lookup
        if subcode in course_lookup:
            info = course_lookup[subcode]
            data.append({
                "Date": d.get('date'),
                "Period": d.get('period', 'N/A'),
                "Dept": dept,
                "Sem": info['sem'],
                "Section": d.get('section'),
                "Subject Code": subcode,
                "Subject Title": info['title'],
                "Faculty Name": d.get('faculty_name'),
                "Absentees Count": len(d.get('absentees', [])),
                "Absent USNs": ", ".join(d.get('absentees', []))
            })
            
    return pd.DataFrame(data)

def generate_student_summary_report(dept, sem, section):
    """
    2. VTU Detention Report.
    Columns: AY, Dept, Sem, Sec, USN, Name, Subject, Held, Attended, %, Status, Signatures
    """
    # A. Get Students
    students = get_students_cached(dept, sem, section)
    if not students: 
        return pd.DataFrame()
    
    report_data = []
    
    # B. Iterate Students
    for s in students:
        usn = s['usn']
        name = s.get('name', 'Unknown')
        ay = s.get('ay', '2025_26')
        
        # C. Get Attendance Stats
        doc = db.collection('Student_Summaries').document(usn).get()
        if not doc.exists: 
            continue
        
        raw_data = doc.to_dict()
        
        # D. Un-flatten Structure (Handle "BEC301.total" format vs Nested)
        structured = {}
        for k, v in raw_data.items():
            if "." in k:
                parts = k.split('.')
                code, field = parts[0], parts[1]
                if code not in structured: structured[code] = {}
                structured[code][field] = v
            elif isinstance(v, dict):
                structured[k] = v
                
        # E. Build Rows for Report
        for code, stats in structured.items():
            # Only report if classes have been held
            if 'total' in stats and stats['total'] > 0:
                tot = stats['total']
                att = stats.get('attended', 0)
                pct = (att / tot * 100)
                status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
                
                report_data.append({
                    "AY": ay,
                    "Dept": dept,
                    "Sem": sem,
                    "Section": section,
                    "USN": usn,
                    "Name": name,
                    "Subject Code": code,
                    "Subject Title": stats.get('title', code),
                    "Classes Held": tot,       # Int for Excel
                    "Classes Attended": att,   # Int for Excel
                    "Percentage": round(pct, 2),
                    "Status": status,
                    "Student Sign": "", # Placeholder for print
                    "Parent Sign": ""   # Placeholder for print
                })
                
    # F. Sort & Return
    if report_data:
        df = pd.DataFrame(report_data)
        # Sort by USN first, then Subject Code
        return df.sort_values(by=['USN', 'Subject Code'])
        
    return pd.DataFrame()

# ==========================================
# 5. UI COMPONENTS (Reusable)
# ==========================================

def render_report_tab():
    """
    Shared Report UI for Admin and Faculty.
    Allows downloading reports with Dropdown selections.
    """
    st.header("📊 Attendance Reports")
    
    # --- Report 1: VTU List ---
    st.subheader("1. 🎓 VTU Shortage/Detention List")
    st.info("Download the mandatory attendance report for students/parents.")
    
    c1, c2, c3 = st.columns(3)
    # Dropdowns for easier selection
    s_dept = c1.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE", "Basic Science"], index=0)
    s_sem = c2.selectbox("Semester", ["1", "2", "3", "4", "5", "6", "7", "8"], index=2)
    s_sec = c3.selectbox("Section", ["A", "B", "C", "D", "E", "F", "G"], index=0)
    
    if st.button("Generate Detention List"):
        with st.spinner("Processing..."):
            df = generate_student_summary_report(s_dept, s_sem, s_sec)
        
        if not df.empty:
            st.success(f"Generated report for {len(df)} records.")
            st.dataframe(df.head())
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="⬇️ Download VTU Format CSV",
                data=csv,
                file_name=f"VTU_Attendance_{s_dept}_{s_sem}_{s_sec}.csv",
                mime="text/csv"
            )
        else:
            st.warning("No data found for this selection.")

    st.divider()
    
    # --- Report 2: Logs ---
    st.subheader("2. 📝 Class Log (Audit)")
    c1, c2, c3 = st.columns(3)
    l_dept = c1.selectbox("Log Dept", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE"], index=0)
    l_start = c2.date_input("From Date", datetime.date.today().replace(day=1))
    l_end = c3.date_input("To Date", datetime.date.today())
    
    if st.button("Generate Class Logs"):
        df = generate_session_report(l_dept, l_start, l_end)
        if not df.empty:
            st.dataframe(df)
            st.download_button("Download Logs", df.to_csv(index=False).encode('utf-8'), "class_logs.csv", "text/csv")
        else:
            st.warning("No classes found.")

# ==========================================
# 6. CSV BATCH PROCESSORS
# ==========================================

def process_courses_csv(df):
    """Batch Upload Courses CSV"""
    # Normalize headers
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    # Renaming map for flexibility
    rename_map = {
        'email': 'facultyemail', 'mail': 'facultyemail',
        'sub': 'subcode', 'code': 'subcode',
        'faculty': 'facultyname', 'fac': 'facultyname',
        'sec': 'section', 'semester': 'sem'
    }
    df = df.rename(columns=rename_map).fillna("")
    
    if 'subcode' not in df.columns:
        return 0, ["❌ Error: 'SubCode' column missing."]

    batch = db.batch()
    count = 0
    logs = []
    
    for _, row in df.iterrows():
        raw_code = row.get('subcode', '')
        if not raw_code: continue
        
        subcode = sanitize_key(raw_code)
        ay = str(row.get('ay', '2025_26')).strip()
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        section = str(row.get('section', 'A')).upper().strip()
        fname = str(row.get('facultyname', 'Faculty')).strip()
        femail = generate_email(fname, row.get('facultyemail', ''))
        
        cid = f"{ay}_{dept}_{sem}_{section}_{subcode}"
        
        # 1. Course Doc
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode, "subtitle": str(row.get('subtitle', subcode)),
            "faculty_id": femail, "faculty_name": fname
        })
        
        # 2. User Doc (Login)
        batch.set(db.collection('Users').document(femail), {
            "name": fname, "role": "Faculty", "dept": dept, "password": "password123"
        }, merge=True)
        
        logs.append(f"Linked {subcode} -> {femail}")
        count += 1
        
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count, logs

def process_students_csv(df):
    """Batch Upload Students CSV"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    df = df.rename(columns={'sec': 'section', 'semester': 'sem', 'academic': 'ay'}).fillna("")
    
    if 'usn' not in df.columns: return 0
    
    batch = db.batch()
    count = 0
    
    # Pre-fetch Courses to Auto-Link
    course_map = {}
    for c in db.collection('Courses').stream():
        d = c.to_dict()
        k = f"{d['dept']}_{d['sem']}_{d['section']}"
        if k not in course_map: course_map[k] = []
        course_map[k].append(d)
        
    for _, row in df.iterrows():
        raw_usn = row.get('usn', '')
        if not raw_usn: continue
        
        usn = sanitize_key(raw_usn)
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        sec = str(row.get('section', 'A')).upper().strip()
        ay = str(row.get('ay', '2025_26')).strip()
        
        # 1. Student Doc
        batch.set(db.collection('Students').document(usn), {
            "name": row.get('name', 'Student'),
            "dept": dept, "sem": sem, "section": sec, "ay": ay, 
            "batch": str(row.get('batch', ''))
        })
        
        # 2. Link Subjects (With SAFE Initialization)
        k = f"{dept}_{sem}_{sec}"
        if k in course_map:
            updates = {}
            for subj in course_map[k]:
                code = sanitize_key(subj['subcode'])
                updates[f"{code}.title"] = subj['subtitle']
                # NEW: Initialize to 0 if missing, but DO NOT overwrite if exists
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            
            if updates: 
                batch.set(db.collection('Student_Summaries').document(usn), updates, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

def admin_force_sync():
    """
    Admin Tool: Fix Links
    Ensures every student has the correct subject keys initialized in Student_Summaries.
    """
    students = db.collection('Students').stream()
    courses = list(db.collection('Courses').stream())
    
    course_map = {}
    for c in courses:
        d = c.to_dict()
        k = f"{str(d['dept']).strip().upper()}_{str(d['sem']).strip()}_{str(d['section']).strip().upper()}"
        if k not in course_map: course_map[k] = []
        course_map[k].append(d)
        
    batch = db.batch()
    count = 0
    updated = 0
    
    for s in students:
        s_data = s.to_dict()
        usn = s.id
        k = f"{str(s_data.get('dept','')).strip().upper()}_{str(s_data.get('sem','')).strip()}_{str(s_data.get('section','')).strip().upper()}"
        
        if k in course_map:
            updates = {}
            for c in course_map[k]:
                code = sanitize_key(c['subcode'])
                updates[f"{code}.title"] = c['subtitle']
                # Using Increment(0) ensures field existence without adding value
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            
            batch.set(db.collection('Student_Summaries').document(usn), updates, merge=True)
            updated += 1
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return updated

# ==========================================
# 7. DASHBOARDS
# ==========================================

def faculty_dashboard(user):
    st.title(f"👨‍🏫 {user['name']}")
    
    my_courses = get_faculty_courses(user['id'])
    
    # Tabs: Attendance | History | Reports (NEW)
    t1, t2, t3 = st.tabs(["📝 Attendance", "📜 History", "📊 Reports"])
    
    # --- Tab 1: Mark Attendance ---
    with t1:
        if not my_courses:
            st.warning("No courses linked to your email.")
        else:
            c_map = {f"{c['subcode']} ({c['section']})" : c for c in my_courses}
            sel_name = st.selectbox("Select Class", list(c_map.keys()))
            course = c_map[sel_name]
            
            st.subheader(f"{course['subcode']} - {course['subtitle']}")
            
            c_date, c_period = st.columns(2)
            date_val = c_date.date_input("Date", datetime.date.today())
            period_val = c_period.selectbox("Period", ["1st Hour", "2nd Hour", "3rd Hour", "4th Hour", "5th Hour", "6th Hour", "7th Hour", "Lab"])
            
            session_id = f"{date_val}_{course['subcode']}_{course['section']}_{period_val}"
            existing_doc = db.collection('Class_Sessions').document(session_id).get()
            already_marked = existing_doc.exists
            
            if already_marked:
                st.error(f"⚠️ {period_val} on {date_val} ALREADY MARKED.")
                if not st.checkbox("Allow Overwrite? (Stats won't increment)"): 
                    st.stop()
            
            if st.button("🔄 Refresh"): 
                get_students_cached.clear()
                st.rerun()
                
            s_list = sorted(get_students_cached(course['dept'], course['sem'], course['section']), key=lambda x: x['usn'])
            
            if not s_list: 
                st.error("No students found.")
            else:
                with st.form("mark"):
                    proxy_name = st.text_input("Faculty", value=user['name'])
                    st.write(f"**Total: {len(s_list)}**")
                    select_all = st.checkbox("Select All", value=True)
                    
                    cols = st.columns(4)
                    status_map = {}
                    for i, s in enumerate(s_list):
                        ukey = f"{s['usn']}_{date_val}_{period_val}" 
                        status_map[s['usn']] = cols[i%4].checkbox(s['usn'], value=select_all, key=ukey)
                    
                    if st.form_submit_button("Submit"):
                        absentees = [u for u, p in status_map.items() if not p]
                        batch = db.batch()
                        
                        # Log Session
                        batch.set(db.collection('Class_Sessions').document(session_id), {
                            "course_code": course['subcode'], 
                            "section": course['section'], 
                            "date": str(date_val),
                            "period": period_val, 
                            "faculty_id": user['id'], 
                            "faculty_name": proxy_name,
                            "absentees": absentees, 
                            "timestamp": datetime.datetime.now()
                        })
                        
                        # Update Stats (Only if new session)
                        if not already_marked:
                            sub_key = sanitize_key(course['subcode'])
                            for s in s_list:
                                ref = db.collection('Student_Summaries').document(s['usn'])
                                batch.set(ref, {
                                    f"{sub_key}.title": course['subtitle'], 
                                    f"{sub_key}.total": firestore.Increment(1)
                                }, merge=True)
                                if s['usn'] not in absentees:
                                    batch.set(ref, {
                                        f"{sub_key}.attended": firestore.Increment(1)
                                    }, merge=True)
                            st.success("Saved!")
                        else:
                            st.warning("Updated log only (Stats not incremented).")
                        batch.commit()
                
    # --- Tab 2: History ---
    with t2:
        logs = list(db.collection('Class_Sessions').where("faculty_id", "==", user['id']).stream())
        data = [{
            "date":l.to_dict().get('date'), 
            "period":l.to_dict().get('period','N/A'), 
            "course":l.to_dict().get('course_code'), 
            "section":l.to_dict().get('section'), 
            "timestamp":l.to_dict().get('timestamp')
        } for l in logs]
        
        data.sort(key=lambda x: x.get('timestamp',''), reverse=True)
        if data: 
            st.dataframe(pd.DataFrame(data)[['date', 'period', 'course', 'section']], use_container_width=True)
        else: 
            st.info("No history.")
            
    # --- Tab 3: Reports (NEW) ---
    with t3:
        render_report_tab()

def student_dashboard():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Portal</h1>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        usn_input = st.text_input("Enter USN").strip().upper()
        if st.button("Check Attendance"):
            if not usn_input: return
            
            usn = sanitize_key(usn_input)
            doc = db.collection('Student_Summaries').document(usn).get()
            
            if not doc.exists: 
                st.error("USN not found.")
                return
            
            data = doc.to_dict()
            structured = {}
            # Smart Un-flattening
            for k, v in data.items():
                if "." in k:
                    parts = k.split('.')
                    if len(parts)>=2: 
                        if parts[0] not in structured: structured[parts[0]] = {}
                        structured[parts[0]][parts[1]] = v
                elif isinstance(v, dict): 
                    structured[k] = v
            
            rows = []
            for c, s in structured.items():
                if s.get('total', 0) > 0:
                    pct = (s.get('attended',0)/s['total']*100)
                    rows.append({
                        "Subject":c, 
                        "Title":s.get('title',c), 
                        "Classes":f"{s.get('attended',0)}/{s['total']}", 
                        "Percentage":pct, 
                        "Status": "Safe" if pct>=85 else ("Warning" if pct>=75 else "Critical")
                    })
            
            if rows:
                df = pd.DataFrame(rows)
                st.divider()
                st.metric("Avg Attendance", f"{df['Percentage'].mean():.1f}%")
                
                c = alt.Chart(df).mark_bar().encode(
                    x='Subject', 
                    y=alt.Y('Percentage', scale=alt.Scale(domain=[0,100])), 
                    color=alt.Color('Percentage', scale=alt.Scale(domain=[0,75,85,100], range=['red','orange','green','green'])), 
                    tooltip=['Title','Classes','Percentage']
                )
                st.altair_chart(c, use_container_width=True)
                st.dataframe(df, use_container_width=True)
            else: 
                st.warning("No data linked.")

def admin_dashboard():
    st.title("⚙️ Admin Dashboard")
    t1, t2, t3, t4, t5 = st.tabs(["📤 Uploads", "🔧 Tools", "📊 Reports", "👨‍🏫 Faculty", "🎓 Students"])
    
    with t1:
        c1, c2 = st.columns(2)
        with c1:
            st.write("#### 1. Courses (Part A)")
            f1 = st.file_uploader("Courses CSV", type='csv', key='a')
            if f1 and st.button("Process Courses"):
                c, logs = process_courses_csv(pd.read_csv(f1))
                st.success(f"Processed {c} courses.")
                st.expander("Logs").write(logs)
        with c2:
            st.write("#### 2. Students (Part B)")
            f2 = st.file_uploader("Students CSV", type='csv', key='b')
            if f2 and st.button("Process Students"):
                c = process_students_csv(pd.read_csv(f2))
                st.success(f"Registered {c} students.")

    with t2:
        st.subheader("🛠️ Maintenance Tools")
        if st.button("🔄 Sync/Fix All"):
            with st.spinner("Syncing..."): 
                n = admin_force_sync()
            st.success(f"Synced {n} students.")
        
        st.divider()
        st.subheader("🕵️ Debug USN")
        d_usn = st.text_input("Debug USN").strip().upper()
        if st.button("Inspect"):
            usn = sanitize_key(d_usn)
            doc = db.collection('Students').document(usn).get()
            if doc.exists:
                st.write("Profile:", doc.to_dict())
                summ = db.collection('Student_Summaries').document(usn).get()
                st.write("Linked:", summ.to_dict() if summ.exists else "None")
            else: 
                st.error("Not found.")

    with t3:
        # Shared Report UI
        render_report_tab()

    with t4:
        if st.button("Load Faculty"):
            docs = db.collection('Users').where("role", "==", "Faculty").stream()
            st.dataframe(pd.DataFrame([d.to_dict() for d in docs]))

    with t5:
        c1, c2, c3 = st.columns(3)
        dept = c1.text_input("D", "ECE"); sem = c2.text_input("S", "3"); sec = c3.text_input("Sec", "A")
        if st.button("Search"):
            st.dataframe(pd.DataFrame(get_students_cached(dept, sem, sec)))
            get_students_cached.clear()

# ==========================================
# 8. MAIN ROUTER
# ==========================================

def main():
    with st.sidebar:
        st.title("🔐 Login")
        if st.session_state['auth_user']:
            st.success(f"User: {st.session_state['auth_user']['name']}")
            if st.button("Logout"): 
                st.session_state['auth_user'] = None
                st.rerun()
        else:
            uid = st.text_input("Email/ID"); pwd = st.text_input("Password", type="password")
            if st.button("Sign In"):
                if uid=="admin" and pwd=="admin123":
                    st.session_state['auth_user'] = {"id":"admin", "name":"Admin", "role":"Admin"}
                    st.rerun()
                else:
                    u = db.collection('Users').document(uid).get()
                    if u.exists and u.to_dict().get('password')==pwd:
                        st.session_state['auth_user'] = {**u.to_dict(), "id":uid}
                        st.rerun()
                    else: 
                        st.error("Invalid")

    user = st.session_state['auth_user']
    if user:
        if user['role'] == "Admin": 
            admin_dashboard()
        elif user['role'] == "Faculty": 
            faculty_dashboard(user)
    else: 
        student_dashboard()

if __name__ == "__main__":
    main()
