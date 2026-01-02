import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import altair as alt
import re

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

st.set_page_config(page_title="VTU Attendance System", page_icon="🎓", layout="wide")

# Session State
if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# Initialize Firebase (Crash-Proof)
if not firebase_admin._apps:
    try:
        # Check for Cloud Secrets first
        if "firebase" in st.secrets:
            key_dict = dict(st.secrets["firebase"])
            cred = credentials.Certificate(key_dict)
        # Fallback to local file
        else:
            cred = credentials.Certificate("firebase_key.json")
        firebase_admin.initialize_app(cred)
    except Exception as e:
        pass

db = firestore.client()

# ==========================================
# 2. CACHING & OPTIMIZATION
# ==========================================

@st.cache_data(ttl=3600)
def get_students_cached(dept, sem, section):
    """Fetches student list from DB."""
    docs = db.collection('Students')\
        .where("dept", "==", dept)\
        .where("sem", "==", sem)\
        .where("section", "==", section).stream()
    
    return [{"usn": d.id, **d.to_dict()} for d in docs]

@st.cache_data(ttl=600)
def get_faculty_courses(faculty_id):
    """Fetches courses assigned to logged-in faculty."""
    docs = db.collection('Courses').where("faculty_id", "==", faculty_id).stream()
    return [d.to_dict() for d in docs]

# ==========================================
# 3. DATA HELPERS
# ==========================================

def sanitize_key(val):
    if not val: return ""
    return str(val).strip().upper().replace(".", "_").replace("/", "_")

def generate_email(name, existing_email=None):
    val = str(existing_email).strip().lower()
    if val and val not in ['nan', 'none', '']:
        return val
    clean_name = re.sub(r'[^a-zA-Z0-9]', '.', str(name).strip().lower())
    clean_name = re.sub(r'\.+', '.', clean_name).strip('.')
    return f"{clean_name}@amc.edu"

# ==========================================
# 4. BATCH PROCESSING (CSV)
# ==========================================

def process_courses_csv(df):
    """Part A: Upload Courses"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
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
        
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode,
            "subtitle": str(row.get('subtitle', subcode)),
            "faculty_id": femail,
            "faculty_name": fname
        })
        
        user_ref = db.collection('Users').document(femail)
        batch.set(user_ref, {
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
    """Part B: Upload Students"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    rename_map = {'sec': 'section', 'semester': 'sem', 'academic': 'ay'}
    df = df.rename(columns=rename_map).fillna("")
    
    if 'usn' not in df.columns: return 0
    
    batch = db.batch()
    count = 0
    
    course_map = {}
    all_courses = db.collection('Courses').stream()
    for c in all_courses:
        d = c.to_dict()
        key = f"{d['dept']}_{d['sem']}_{d['section']}"
        if key not in course_map: course_map[key] = []
        course_map[key].append(d)
        
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
            "dept": dept, "sem": sem, "section": sec,
            "ay": ay, "batch": str(row.get('batch', ''))
        })
        
        class_key = f"{dept}_{sem}_{sec}"
        if class_key in course_map:
            summ_ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for subj in course_map[class_key]:
                s_code = sanitize_key(subj['subcode'])
                updates[f"{s_code}.title"] = subj['subtitle']
            
            if updates:
                batch.set(summ_ref, updates, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

def admin_force_sync():
    """Sync tool for missing subjects."""
    students = db.collection('Students').stream()
    courses = list(db.collection('Courses').stream())
    
    course_map = {}
    for c in courses:
        d = c.to_dict()
        k = f"{d['dept']}_{d['sem']}_{d['section']}"
        if k not in course_map: course_map[k] = []
        course_map[k].append(d)
        
    batch = db.batch()
    count = 0
    updated = 0
    
    for s in students:
        s_data = s.to_dict()
        usn = s.id
        k = f"{s_data.get('dept', '')}_{s_data.get('sem', '')}_{s_data.get('section', '')}"
        
        if k in course_map:
            ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for c in course_map[k]:
                code = sanitize_key(c['subcode'])
                updates[f"{code}.title"] = c['subtitle']
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            
            batch.set(ref, updates, merge=True)
            updated += 1
        
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return updated

# ==========================================
# 5. DASHBOARDS
# ==========================================

def faculty_dashboard(user):
    st.title(f"👨‍🏫 {user['name']}")
    
    my_courses = get_faculty_courses(user['id'])
    
    if not my_courses:
        st.warning("No courses linked to your email.")
        return
        
    c_map = {f"{c['subcode']} ({c['section']})" : c for c in my_courses}
    sel_name = st.selectbox("Select Class", list(c_map.keys()))
    course = c_map[sel_name]
    
    t1, t2 = st.tabs(["📝 Attendance", "📜 History"])
    
    with t1:
        st.subheader(f"{course['subcode']} - {course['subtitle']}")
        
        # --- NEW: TIME SLOT SELECTION ---
        c_date, c_period = st.columns(2)
        date_val = c_date.date_input("Date", datetime.date.today())
        period_val = c_period.selectbox("Period / Hour", ["1st Hour", "2nd Hour", "3rd Hour", "4th Hour", "5th Hour", "6th Hour", "7th Hour", "Lab Session"])
        
        # --- LOGIC: Check Duplicates ---
        # Unique ID: 2024-01-01_18CS51_A_1st Hour
        session_id = f"{date_val}_{course['subcode']}_{course['section']}_{period_val}"
        
        # Check if this exists in DB
        existing_doc = db.collection('Class_Sessions').document(session_id).get()
        already_marked = existing_doc.exists
        
        if already_marked:
            st.error(f"⚠️ Attendance for **{period_val}** on {date_val} is ALREADY MARKED.")
            overwrite = st.checkbox("I made a mistake. Allow Overwrite?")
            if not overwrite:
                st.stop() # Stop execution here to prevent accidental double submit
        
        # --- LOAD STUDENTS ---
        if st.button("🔄 Refresh List"):
            get_students_cached.clear()
            st.rerun()
            
        s_list = get_students_cached(course['dept'], course['sem'], course['section'])
        s_list = sorted(s_list, key=lambda x: x['usn'])
        
        if not s_list:
            st.error("No students found in this section.")
            return
            
        with st.form("mark_attendance"):
            proxy_name = st.text_input("Faculty Name (if proxy)", value=user['name'])
            
            st.write(f"**Total Students: {len(s_list)}**")
            select_all = st.checkbox("Select All", value=True)
            
            cols = st.columns(4)
            status_map = {}
            
            for i, s in enumerate(s_list):
                ukey = f"{s['usn']}_{date_val}_{period_val}" # Unique key per student per slot
                status_map[s['usn']] = cols[i%4].checkbox(s['usn'], value=select_all, key=ukey)
            
            if st.form_submit_button("🚀 Submit Attendance"):
                absentees = [u for u, present in status_map.items() if not present]
                
                batch = db.batch()
                
                # A. Log Session (Using Custom ID to prevent duplicates)
                log_ref = db.collection('Class_Sessions').document(session_id)
                
                batch.set(log_ref, {
                    "course_code": course['subcode'],
                    "section": course['section'],
                    "date": str(date_val),
                    "period": period_val,  # <--- NEW
                    "faculty_id": user['id'],
                    "faculty_name": proxy_name,
                    "absentees": absentees,
                    "timestamp": datetime.datetime.now()
                })
                
                # B. Update Stats
                # NOTE: If we are Overwriting, we ideally need to "Subtract" previous logic first.
                # However, calculating "Net Change" is complex. 
                # Current Logic: Simple Increment.
                # WARNING: Overwrite feature here simply updates the LOG. It does NOT correct the Student Summary count (which keeps adding).
                # To fix Summary on Overwrite is very hard without a transaction.
                # Recommendation: Disable Overwrite for 'Stats', allow only for 'Log'.
                
                if already_marked:
                    st.warning("Session updated. Note: Student total counts were NOT incremented again to prevent double counting.")
                else:
                    # Only increment totals if this is a NEW session
                    sub_key = sanitize_key(course['subcode'])
                    for s in s_list:
                        summ_ref = db.collection('Student_Summaries').document(s['usn'])
                        
                        batch.set(summ_ref, {
                            f"{sub_key}.title": course['subtitle'],
                            f"{sub_key}.total": firestore.Increment(1)
                        }, merge=True)
                        
                        if s['usn'] not in absentees:
                            batch.set(summ_ref, {
                                f"{sub_key}.attended": firestore.Increment(1)
                            }, merge=True)
                        
                    st.success("Attendance Saved Successfully!")
                    
                batch.commit()
                
    with t2:
        logs = list(db.collection('Class_Sessions').where("faculty_id", "==", user['id']).stream())
        data = [l.to_dict() for l in logs]
        data.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        if data:
            st.dataframe(pd.DataFrame(data)[['date', 'period', 'course_code', 'section', 'faculty_name']], use_container_width=True)
        else:
            st.info("No history found.")

def student_dashboard():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Portal</h1>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        usn_input = st.text_input("Enter USN", placeholder="e.g., 1MV20CS001").strip().upper()
        if st.button("Check Attendance", use_container_width=True):
            if not usn_input: return
            
            usn = sanitize_key(usn_input)
            doc = db.collection('Student_Summaries').document(usn).get()
            
            if not doc.exists:
                st.error("USN not found. Please contact Admin.")
                return
            
            data = doc.to_dict()
            rows = []
            
            for code, stats in data.items():
                if isinstance(stats, dict) and 'total' in stats:
                    tot = stats['total']
                    att = stats.get('attended', 0)
                    pct = (att / tot * 100) if tot > 0 else 0
                    status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
                    
                    rows.append({
                        "Subject": code,
                        "Title": stats.get('title', code),
                        "Percentage": pct,
                        "Status": status,
                        "Classes": f"{att}/{tot}"
                    })
            
            if rows:
                df = pd.DataFrame(rows)
                st.divider()
                
                m1, m2, m3 = st.columns(3)
                m1.metric("Avg Attendance", f"{df['Percentage'].mean():.1f}%")
                m2.metric("Safe Subjects", len(df[df['Status']=='Safe']))
                m3.metric("Critical", len(df[df['Status']=='Critical']))
                
                c = alt.Chart(df).mark_bar().encode(
                    x='Subject',
                    y=alt.Y('Percentage', scale=alt.Scale(domain=[0, 100])),
                    color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None),
                    tooltip=['Title', 'Classes', 'Percentage']
                ).properties(height=300)
                
                st.altair_chart(c, use_container_width=True)
                st.dataframe(df[['Subject', 'Title', 'Classes', 'Percentage', 'Status']], use_container_width=True)
            else:
                st.warning("No subject data linked. Ask Admin to 'Sync Subjects'.")

def admin_dashboard():
    st.title("⚙️ Admin Dashboard")
    
    t1, t2, t3, t4 = st.tabs(["📤 Uploads", "🔧 Tools", "👨‍🏫 Faculty", "🎓 Students"])
    
    with t1:
        c1, c2 = st.columns(2)
        with c1:
            st.write("#### 1. Courses (Part A)")
            f1 = st.file_uploader("CSV: AY, Dept, Sem, Sec, SubCode, Title, Faculty", type='csv', key='a')
            if f1 and st.button("Process Courses"):
                c, logs = process_courses_csv(pd.read_csv(f1))
                st.success(f"Processed {c} courses.")
                st.expander("Logs").write(logs)
        with c2:
            st.write("#### 2. Students (Part B)")
            f2 = st.file_uploader("CSV: USN, Name, Dept, Sem, Sec, AY", type='csv', key='b')
            if f2 and st.button("Process Students"):
                c = process_students_csv(pd.read_csv(f2))
                st.success(f"Registered {c} students.")

    with t2:
        st.subheader("🛠️ Maintenance Tools")
        if st.button("🔄 Sync/Fix All Student Subjects"):
            with st.spinner("Scanning..."):
                n = admin_force_sync()
            st.success(f"Synced subjects for {n} students.")

    with t3:
        if st.button("Load Faculty"):
            docs = db.collection('Users').where("role", "==", "Faculty").stream()
            st.dataframe(pd.DataFrame([d.to_dict() for d in docs]))

    with t4:
        c1, c2, c3 = st.columns(3)
        dept = c1.text_input("Dept", "ECE")
        sem = c2.text_input("Sem", "3")
        sec = c3.text_input("Sec", "A")
        if st.button("Search"):
            st.dataframe(pd.DataFrame(get_students_cached(dept, sem, sec)))
            get_students_cached.clear()

# ==========================================
# 6. MAIN
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
            uid = st.text_input("Email / ID").strip()
            pwd = st.text_input("Password", type="password").strip()
            if st.button("Sign In"):
                if uid == "admin" and pwd == "admin123":
                    st.session_state['auth_user'] = {"id": "admin", "name": "Admin", "role": "Admin"}
                    st.rerun()
                else:
                    u_doc = db.collection('Users').document(uid).get()
                    if u_doc.exists and u_doc.to_dict().get('password') == pwd:
                        u = u_doc.to_dict()
                        u['id'] = uid
                        st.session_state['auth_user'] = u
                        st.rerun()
                    else:
                        st.error("Invalid Credentials")

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
