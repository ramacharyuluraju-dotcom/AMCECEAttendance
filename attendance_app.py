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

# Session State
if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# Initialize Firebase
if not firebase_admin._apps:
    try:
        if "firebase" in st.secrets:
            key_dict = dict(st.secrets["firebase"])
            cred = credentials.Certificate(key_dict)
        else:
            cred = credentials.Certificate("firebase_key.json")
        firebase_admin.initialize_app(cred)
    except Exception as e:
        pass

db = firestore.client()

# ==========================================
# 2. CACHING & OPTIMIZATION
# ==========================================

@st.cache_data(ttl=60) # Reduced TTL for fresher data
def get_students_cached(dept, sem, section):
    """Fetches student list from DB."""
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
    docs = db.collection('Courses').where("faculty_id", "==", faculty_id).stream()
    return [d.to_dict() for d in docs]

# ==========================================
# 3. DATA HELPERS
# ==========================================

def sanitize_key(val):
    if not val: return ""
    return str(val).strip().upper().replace(".", "_").replace("/", "_").replace(" ", "")

def generate_email(name, existing_email=None):
    val = str(existing_email).strip().lower()
    if val and val not in ['nan', 'none', '']:
        return val
    clean_name = re.sub(r'[^a-zA-Z0-9]', '.', str(name).strip().lower())
    clean_name = re.sub(r'\.+', '.', clean_name).strip('.')
    return f"{clean_name}@amc.edu"

# ==========================================
# 4. REPORT GENERATORS (FIXED)
# ==========================================

def generate_session_report(dept, start_date, end_date):
    """Class Log Report"""
    # 1. Build Course Lookup for this Dept
    all_courses = db.collection('Courses').where("dept", "==", dept).stream()
    course_lookup = {}
    for c in all_courses:
        d = c.to_dict()
        course_lookup[d['subcode']] = {
            'sem': d.get('sem', 'N/A'),
            'title': d.get('subtitle', '')
        }

    # 2. Query Sessions
    sessions = db.collection('Class_Sessions')\
        .where("date", ">=", str(start_date))\
        .where("date", "<=", str(end_date))\
        .stream()
        
    data = []
    for s in sessions:
        d = s.to_dict()
        subcode = d.get('course_code', '')
        # Only add if subject belongs to requested Dept
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
    """VTU Detention Report (FIXED: Includes 0/0 students)"""
    # 1. Get List of Students
    students = get_students_cached(dept, sem, section)
    if not students: return pd.DataFrame()
    
    report_data = []
    
    # 2. Iterate ALL students (even those with no attendance yet)
    for s in students:
        usn = s['usn']
        name = s.get('name', 'Unknown')
        ay = s.get('ay', '2025_26')
        
        # 3. Fetch Summary
        doc = db.collection('Student_Summaries').document(usn).get()
        
        structured = {}
        
        if doc.exists:
            raw_data = doc.to_dict()
            # Un-flatten logic
            for k, v in raw_data.items():
                if "." in k:
                    parts = k.split('.')
                    code, field = parts[0], parts[1]
                    if code not in structured: structured[code] = {}
                    structured[code][field] = v
                elif isinstance(v, dict):
                    structured[k] = v
        
        # 4. If no summary exists, look up what courses they SHOULD have
        # This ensures they appear in the report even if they have 0 classes
        if not structured:
            courses = db.collection('Courses')\
                .where("dept", "==", dept)\
                .where("sem", "==", sem)\
                .where("section", "==", section).stream()
            for c in courses:
                cd = c.to_dict()
                sc = sanitize_key(cd['subcode'])
                structured[sc] = {'title': cd['subtitle'], 'total': 0, 'attended': 0}

        # 5. Build Rows
        for code, stats in structured.items():
            tot = stats.get('total', 0)
            att = stats.get('attended', 0)
            absent = tot - att
            
            # Default to 100% if no classes held yet
            if tot == 0: pct = 100.0 
            else: pct = (att / tot * 100)
                
            status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
            
            report_data.append({
                "AY": ay, "Dept": dept, "Sem": sem, "Section": section,
                "USN": usn, "Name": name, 
                "Subject Code": code,
                "Subject Title": stats.get('title', code),
                "Total Classes": tot,         # <--- Explicit
                "Classes Attended": att,      # <--- Explicit
                "Classes Absent": absent,     # <--- Explicit
                "Percentage": round(pct, 2), 
                "Status": status
            })
                
    if report_data:
        df = pd.DataFrame(report_data)
        return df.sort_values(by=['USN', 'Subject Code'])
    return pd.DataFrame()

# ==========================================
# 5. UI COMPONENTS (Shared)
# ==========================================

def render_report_tab():
    st.header("📊 Attendance Reports")
    
    st.subheader("1. 🎓 VTU Shortage/Detention List")
    c1, c2, c3 = st.columns(3)
    s_dept = c1.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE", "Basic Science"], index=0)
    s_sem = c2.selectbox("Semester", ["1", "2", "3", "4", "5", "6", "7", "8"], index=2)
    s_sec = c3.selectbox("Section", ["A", "B", "C", "D", "E", "F", "G"], index=0)
    
    if st.button("Generate Detention List"):
        with st.spinner("Processing..."):
            df = generate_student_summary_report(s_dept, s_sem, s_sec)
        
        if not df.empty:
            st.success(f"Generated report for {len(df)} records.")
            st.dataframe(df)
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Download VTU Format CSV", csv, f"VTU_Attendance_{s_dept}_{s_sem}_{s_sec}.csv", "text/csv")
        else:
            st.warning("No data found for this class.")

    st.divider()
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
# 6. CSV PROCESSORS
# ==========================================

def process_courses_csv(df):
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    rename_map = {'email':'facultyemail','mail':'facultyemail','sub':'subcode','code':'subcode','faculty':'facultyname','fac':'facultyname','sec':'section','semester':'sem'}
    df = df.rename(columns=rename_map).fillna("")
    if 'subcode' not in df.columns: return 0, ["❌ Error: Missing SubCode"]

    batch = db.batch(); count = 0; logs = []
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
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode, "subtitle": str(row.get('subtitle', subcode)),
            "faculty_id": femail, "faculty_name": fname
        })
        batch.set(db.collection('Users').document(femail), {
            "name": fname, "role": "Faculty", "dept": dept, "password": "password123"
        }, merge=True)
        logs.append(f"Linked {subcode} -> {femail}")
        count += 1
        if count % 200 == 0: batch.commit(); batch = db.batch()
    batch.commit()
    return count, logs

def process_students_csv(df):
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    df = df.rename(columns={'sec': 'section', 'semester': 'sem', 'academic': 'ay'}).fillna("")
    if 'usn' not in df.columns: return 0
    
    batch = db.batch(); count = 0
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
        
        batch.set(db.collection('Students').document(usn), {
            "name": row.get('name', 'Student'),
            "dept": dept, "sem": sem, "section": sec, "ay": ay, "batch": str(row.get('batch', ''))
        })
        k = f"{dept}_{sem}_{sec}"
        if k in course_map:
            updates = {}
            for subj in course_map[k]:
                code = sanitize_key(subj['subcode'])
                updates[f"{code}.title"] = subj['subtitle']
                # Safe init: Create fields but do not overwrite values
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            if updates: batch.set(db.collection('Student_Summaries').document(usn), updates, merge=True)
        count += 1
        if count % 200 == 0: batch.commit(); batch = db.batch()
    batch.commit()
    return count

def admin_force_sync():
    students = db.collection('Students').stream()
    courses = list(db.collection('Courses').stream())
    course_map = {}
    for c in courses:
        d = c.to_dict()
        k = f"{str(d['dept']).strip().upper()}_{str(d['sem']).strip()}_{str(d['section']).strip().upper()}"
        if k not in course_map: course_map[k] = []
        course_map[k].append(d)
    
    batch = db.batch(); count = 0; updated = 0
    for s in students:
        s_data = s.to_dict(); usn = s.id
        k = f"{str(s_data.get('dept','')).strip().upper()}_{str(s_data.get('sem','')).strip()}_{str(s_data.get('section','')).strip().upper()}"
        if k in course_map:
            updates = {}
            for c in course_map[k]:
                code = sanitize_key(c['subcode'])
                updates[f"{code}.title"] = c['subtitle']
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            batch.set(db.collection('Student_Summaries').document(usn), updates, merge=True)
            updated += 1
        count += 1
        if count % 200 == 0: batch.commit(); batch = db.batch()
    batch.commit()
    return updated

# ==========================================
# 7. DASHBOARDS
# ==========================================

def faculty_dashboard(user):
    st.title(f"👨‍🏫 {user['name']}")
    my_courses = get_faculty_courses(user['id'])
    
    t1, t2, t3 = st.tabs(["📝 Attendance", "📜 History", "📊 Reports"])
    
    with t1:
        if not my_courses: st.warning("No courses linked.")
        else:
            c_map = {f"{c['subcode']} ({c['section']})" : c for c in my_courses}
            sel_name = st.selectbox("Select Class", list(c_map.keys()))
            course = c_map[sel_name]
            
            st.subheader(f"{course['subcode']} - {course['subtitle']}")
            c_date, c_period = st.columns(2)
            date_val = c_date.date_input("Date", datetime.date.today())
            period_val = c_period.selectbox("Period", ["1st Hour", "2nd Hour", "3rd Hour", "4th Hour", "5th Hour", "6th Hour", "7th Hour", "Lab"])
            
            session_id = f"{date_val}_{course['subcode']}_{course['section']}_{period_val}"
            already_marked = db.collection('Class_Sessions').document(session_id).get().exists
            
            if already_marked:
                st.error("ALREADY MARKED.")
                if not st.checkbox("Allow Overwrite? (Stats won't increment)"): st.stop()
            
            if st.button("🔄 Refresh"): get_students_cached.clear(); st.rerun()
            s_list = sorted(get_students_cached(course['dept'], course['sem'], course['section']), key=lambda x: x['usn'])
            
            if not s_list: st.error("No students found.")
            else:
                with st.form("mark"):
                    proxy_name = st.text_input("Faculty", value=user['name'])
                    st.write(f"**Total: {len(s_list)}**")
                    select_all = st.checkbox("Select All", value=True)
                    cols = st.columns(4); status_map = {}
                    for i, s in enumerate(s_list):
                        ukey = f"{s['usn']}_{date_val}_{period_val}" 
                        status_map[s['usn']] = cols[i%4].checkbox(s['usn'], value=select_all, key=ukey)
                    
                    if st.form_submit_button("Submit"):
                        absentees = [u for u, p in status_map.items() if not p]
                        batch = db.batch()
                        batch.set(db.collection('Class_Sessions').document(session_id), {
                            "course_code": course['subcode'], "section": course['section'], "date": str(date_val),
                            "period": period_val, "faculty_id": user['id'], "faculty_name": proxy_name,
                            "absentees": absentees, "timestamp": datetime.datetime.now()
                        })
                        if not already_marked:
                            sub_key = sanitize_key(course['subcode'])
                            for s in s_list:
                                ref = db.collection('Student_Summaries').document(s['usn'])
                                batch.set(ref, {f"{sub_key}.title": course['subtitle'], f"{sub_key}.total": firestore.Increment(1)}, merge=True)
                                if s['usn'] not in absentees: batch.set(ref, {f"{sub_key}.attended": firestore.Increment(1)}, merge=True)
                            st.success("Saved!")
                        else: st.warning("Updated log only.")
                        batch.commit()
    with t2:
        logs = list(db.collection('Class_Sessions').where("faculty_id", "==", user['id']).stream())
        data = [{"date":l.to_dict().get('date'), "period":l.to_dict().get('period','N/A'), "course":l.to_dict().get('course_code'), "section":l.to_dict().get('section')} for l in logs]
        if data: st.dataframe(pd.DataFrame(data)); 
        else: st.info("No history.")
    with t3:
        # FACULTY CAN NOW SEE REPORTS TOO
        render_report_tab()

def student_dashboard():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Portal</h1>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        usn_input = st.text_input("Enter USN").strip().upper()
        if st.button("Check Attendance"):
            if not usn_input: return
            usn = sanitize_key(usn_input)
            doc = db.collection('Student_Summaries').document(usn).get()
            if not doc.exists: st.error("USN not found."); return
            
            data = doc.to_dict(); structured = {}
            for k, v in data.items():
                if "." in k:
                    parts = k.split('.')
                    if len(parts)>=2: 
                        if parts[0] not in structured: structured[parts[0]] = {}
                        structured[parts[0]][parts[1]] = v
                elif isinstance(v, dict): structured[k] = v
            
            rows = []
            for c, s in structured.items():
                tot = s.get('total', 0); att = s.get('attended', 0)
                pct = 100.0 if tot == 0 else (att/tot*100)
                rows.append({"Subject":c, "Title":s.get('title',c), "Classes":f"{att}/{tot}", "Percentage":pct, "Status": "Safe" if pct>=85 else ("Warning" if pct>=75 else "Critical")})
            
            if rows:
                df = pd.DataFrame(rows)
                st.divider(); st.metric("Avg Attendance", f"{df['Percentage'].mean():.1f}%")
                c = alt.Chart(df).mark_bar().encode(x='Subject', y=alt.Y('Percentage', scale=alt.Scale(domain=[0,100])), color=alt.Color('Percentage', scale=alt.Scale(domain=[0,75,85,100], range=['red','orange','green','green'])))
                st.altair_chart(c, use_container_width=True)
                st.dataframe(df, use_container_width=True)
            else: st.warning("No data linked.")

def admin_dashboard():
    st.title("⚙️ Admin Dashboard")
    t1, t2, t3, t4, t5 = st.tabs(["📤 Uploads", "🔧 Tools", "📊 Reports", "👨‍🏫 Faculty", "🎓 Students"])
    
    with t1:
        c1, c2 = st.columns(2)
        with c1:
            st.write("#### 1. Bulk Courses")
            f1 = st.file_uploader("Courses CSV", type='csv', key='a')
            if f1 and st.button("Process Courses"):
                c, logs = process_courses_csv(pd.read_csv(f1))
                st.success(f"Processed {c} courses."); st.expander("Logs").write(logs)
        with c2:
            st.write("#### 2. Bulk Students")
            f2 = st.file_uploader("Students CSV", type='csv', key='b')
            if f2 and st.button("Process Students"):
                c = process_students_csv(pd.read_csv(f2))
                st.success(f"Registered {c} students.")

    with t2:
        if st.button("🔄 Sync/Fix All"):
            with st.spinner("Syncing..."): n = admin_force_sync()
            st.success(f"Synced {n} students.")
        st.divider()
        d_usn = st.text_input("Debug USN").strip().upper()
        if st.button("Inspect"):
            d = db.collection('Students').document(sanitize_key(d_usn)).get()
            st.write(d.to_dict() if d.exists else "Not Found")

    with t3:
        render_report_tab()

    with t4:
        st.subheader("Manage Faculty")
        c1, c2 = st.columns(2)
        with c1.form("add_fac"):
            st.write("Add New Faculty")
            n_name = st.text_input("Name"); n_email = st.text_input("Email (ID)"); n_dept = st.text_input("Dept"); n_pass = st.text_input("Password", type="password")
            if st.form_submit_button("Create Faculty"):
                if n_email and n_pass:
                    db.collection('Users').document(n_email).set({"name":n_name, "role":"Faculty", "dept":n_dept, "password":n_pass})
                    st.success("Created")
        
        with c2:
            st.write("Existing Faculty")
            facs = list(db.collection('Users').where("role", "==", "Faculty").stream())
            if facs:
                df_fac = pd.DataFrame([{"Email/ID": f.id, **f.to_dict()} for f in facs])
                st.dataframe(df_fac[["Email/ID", "name", "dept"]], use_container_width=True)
                
                st.divider()
                st.write("⚠️ **Safe Delete**")
                to_del = st.selectbox("Select Faculty to Remove", [f.id for f in facs])
                
                if st.button("Remove Faculty"):
                    # Unassign courses (Safe Delete)
                    courses = db.collection('Courses').where("faculty_id", "==", to_del).stream()
                    batch = db.batch()
                    count = 0
                    for c in courses:
                        batch.update(c.reference, {"faculty_id": "UNASSIGNED", "faculty_name": "Unassigned"})
                        count += 1
                    
                    batch.delete(db.collection('Users').document(to_del))
                    batch.commit()
                    st.success(f"Removed {to_del}. {count} courses unassigned.")
                    st.rerun()

    with t5:
        st.subheader("Manage Students")
        tab_search, tab_add = st.tabs(["🔍 Search & Edit", "➕ Add New Student"])
        
        with tab_search:
            s_search = st.text_input("Enter USN to Search", placeholder="1MV...").strip().upper()
            if s_search:
                s_doc = db.collection('Students').document(s_search).get()
                if s_doc.exists:
                    d = s_doc.to_dict()
                    st.success(f"Found: {d.get('name')}")
                    with st.form("edit_stu"):
                        c1, c2 = st.columns(2)
                        e_name = c1.text_input("Name", d.get('name', ''))
                        e_dept = c2.text_input("Dept", d.get('dept', ''))
                        e_sem = c1.text_input("Sem", d.get('sem', ''))
                        e_sec = c2.text_input("Section", d.get('section', ''))
                        if st.form_submit_button("Update Student Profile"):
                            db.collection('Students').document(s_search).update({"name": e_name, "dept": e_dept, "sem": e_sem, "section": e_sec})
                            st.success("✅ Profile Updated!")
                    
                    if st.button("❌ Delete Student"):
                        db.collection('Students').document(s_search).delete()
                        db.collection('Student_Summaries').document(s_search).delete()
                        st.error(f"Deleted {s_search}")
                        st.rerun()
                else:
                    st.warning("USN not found.")

        with tab_add:
            st.write("Manually register a student.")
            with st.form("add_student_manual"):
                c1, c2 = st.columns(2)
                m_usn = c1.text_input("USN").strip().upper()
                m_name = c2.text_input("Name")
                m_dept = c1.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE"], index=0)
                m_sem = c2.selectbox("Semester", ["1", "2", "3", "4", "5", "6", "7", "8"], index=2)
                m_sec = c1.text_input("Section", "A").strip().upper()
                m_batch = c2.text_input("Batch", "2025")
                m_ay = "2025_26"
                
                if st.form_submit_button("Add Student"):
                    if m_usn and m_name:
                        db.collection('Students').document(m_usn).set({
                            "name": m_name, "dept": m_dept, "sem": m_sem, 
                            "section": m_sec, "ay": m_ay, "batch": m_batch
                        })
                        # Auto-Link Logic
                        courses = db.collection('Courses').where("dept", "==", m_dept)\
                            .where("sem", "==", m_sem).where("section", "==", m_sec).stream()
                        updates = {}
                        for c in courses:
                            cd = c.to_dict(); sc = sanitize_key(cd['subcode'])
                            updates[f"{sc}.title"] = cd['subtitle']
                            updates[f"{sc}.total"] = firestore.Increment(0)
                            updates[f"{sc}.attended"] = firestore.Increment(0)
                        if updates: db.collection('Student_Summaries').document(m_usn).set(updates, merge=True)
                        st.success(f"✅ Added {m_name}")

def main():
    with st.sidebar:
        st.title("🔐 Login")
        if st.session_state['auth_user']:
            st.success(f"User: {st.session_state['auth_user']['name']}")
            if st.button("Logout"): st.session_state['auth_user'] = None; st.rerun()
        else:
            uid = st.text_input("Email/ID"); pwd = st.text_input("Password", type="password")
            if st.button("Sign In"):
                if uid=="admin" and pwd=="admin123":
                    st.session_state['auth_user'] = {"id":"admin", "name":"Admin", "role":"Admin"}; st.rerun()
                else:
                    u = db.collection('Users').document(uid).get()
                    if u.exists and u.to_dict().get('password')==pwd:
                        st.session_state['auth_user'] = {**u.to_dict(), "id":uid}; st.rerun()
                    else: st.error("Invalid")

    user = st.session_state['auth_user']
    if user:
        if user['role'] == "Admin": admin_dashboard()
        elif user['role'] == "Faculty": faculty_dashboard(user)
    else: student_dashboard()

if __name__ == "__main__":
    main()
