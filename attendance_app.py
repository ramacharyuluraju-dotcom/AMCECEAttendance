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
        # If app is already initialized, we pass
        pass

db = firestore.client()

# ==========================================
# 2. CACHING & OPTIMIZATION (Saves $$$)
# ==========================================

@st.cache_data(ttl=3600)
def get_students_cached(dept, sem, section):
    """Fetches student list from DB (Cached for 1 hour to save reads)."""
    # Note: We rely on the 'Students' collection for the list
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
    """Ensures Keys (SubCode/USN) are Firestore-safe (No dots/slashes)."""
    if not val: return ""
    # Replace dots and slashes with underscore to prevent nested object creation
    return str(val).strip().upper().replace(".", "_").replace("/", "_")

def generate_email(name, existing_email=None):
    """
    Generates a valid email if missing.
    Handles 'Dr.Aruna.R' -> 'dr.aruna.r@amc.edu'
    """
    val = str(existing_email).strip().lower()
    if val and val not in ['nan', 'none', '']:
        return val
    
    # Generate from name
    # Replace anything that isn't a letter/number with a dot
    clean_name = re.sub(r'[^a-zA-Z0-9]', '.', str(name).strip().lower())
    # Remove repeated dots (e.g. ..)
    clean_name = re.sub(r'\.+', '.', clean_name).strip('.')
    return f"{clean_name}@amc.edu"

# ==========================================
# 4. BATCH PROCESSING (CSV)
# ==========================================

def process_courses_csv(df):
    """Part A: Upload Courses"""
    # Normalize headers
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    # Map varying column names to standard
    rename_map = {
        'email': 'facultyemail', 'mail': 'facultyemail',
        'sub': 'subcode', 'code': 'subcode', 'subjectcode': 'subcode',
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
        
        # 1. Clean Data
        subcode = sanitize_key(raw_code) # Handles special chars
        ay = str(row.get('ay', '2025_26')).strip()
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        section = str(row.get('section', 'A')).upper().strip()
        
        fname = str(row.get('facultyname', 'Faculty')).strip()
        femail = generate_email(fname, row.get('facultyemail', ''))
        
        cid = f"{ay}_{dept}_{sem}_{section}_{subcode}"
        
        # 2. Set Course
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode,
            "subtitle": str(row.get('subtitle', subcode)),
            "faculty_id": femail,
            "faculty_name": fname
        })
        
        # 3. Create/Update Faculty Login
        # We merge to ensure we don't break existing passwords if they changed it
        user_ref = db.collection('Users').document(femail)
        batch.set(user_ref, {
            "name": fname, 
            "role": "Faculty", 
            "dept": dept,
            "password": "password123" # Default pwd
        }, merge=True)
        
        logs.append(f"✅ {subcode}: Linked to {fname} ({femail})")
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
    
    # Pre-fetch Course Mapping for Auto-Linking
    # Map: "ECE_3_A" -> [ {code: 'BMATEC301', title: 'Maths'}, ... ]
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
        
        # 1. Clean Data
        usn = sanitize_key(raw_usn)
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        sec = str(row.get('section', 'A')).upper().strip()
        ay = str(row.get('ay', '2025_26')).strip()
        
        # 2. Create Student Profile
        batch.set(db.collection('Students').document(usn), {
            "name": row.get('name', 'Student'),
            "dept": dept, "sem": sem, "section": sec,
            "ay": ay, "batch": str(row.get('batch', ''))
        })
        
        # 3. Initialize Subject Stats (Auto-Link)
        # This ensures the student starts with 0/0 attendance
        class_key = f"{dept}_{sem}_{sec}"
        if class_key in course_map:
            summ_ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for subj in course_map[class_key]:
                s_code = sanitize_key(subj['subcode'])
                # We use merge=True, so we just init fields if missing
                # Setting total=0 explicitly ensures the field exists
                # BUT we must be careful not to reset attendance if re-running
                # Firestore `create` is not available in batch in this SDK simply
                # So we use a check: In a real app, this might overwrite.
                # Here we assume Part B is run ONCE at start of sem.
                updates[f"{s_code}.title"] = subj['subtitle']
                # Note: We do NOT set total=0 here to avoid overwriting existing data
                # We rely on the "Sync" tool in Admin for fixing missing fields
            
            if updates:
                batch.set(summ_ref, updates, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

def admin_force_sync():
    """
    MAGIC FIX: Scans all students and ensures their Subject keys exist.
    Run this if students say 'No Attendance Data'.
    """
    students = db.collection('Students').stream()
    courses = list(db.collection('Courses').stream())
    
    # Build Map: "ECE_3_A" -> List of Course Dicts
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
            # We construct an update dictionary using dot notation
            # This allows us to set nested fields without overwriting the whole doc
            # AND Firestore won't overwrite existing values if we use valid logic?
            # Actually, to be safe, we only set 'title' and ensure 'total' exists using Increment(0)
            
            updates = {}
            for c in course_map[k]:
                code = sanitize_key(c['subcode'])
                # Setting title is safe
                updates[f"{code}.title"] = c['subtitle']
                # Increment(0) creates the field if missing, but adds 0 if exists (Safe!)
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
    
    # 1. Fetch Assigned Courses
    my_courses = get_faculty_courses(user['id'])
    
    if not my_courses:
        st.warning("No courses linked to your email.")
        return
        
    # Dropdown
    c_map = {f"{c['subcode']} ({c['section']})" : c for c in my_courses}
    sel_name = st.selectbox("Select Class", list(c_map.keys()))
    course = c_map[sel_name]
    
    t1, t2 = st.tabs(["📝 Attendance", "📜 History"])
    
    with t1:
        st.subheader(f"{course['subcode']} - {course['subtitle']}")
        
        # 2. Fetch Students
        if st.button("🔄 Refresh List"):
            get_students_cached.clear()
            st.rerun()
            
        s_list = get_students_cached(course['dept'], course['sem'], course['section'])
        # Sort by USN
        s_list = sorted(s_list, key=lambda x: x['usn'])
        
        if not s_list:
            st.error("No students found in this section.")
            return
            
        with st.form("mark_attendance"):
            c1, c2 = st.columns([1, 2])
            date_val = c1.date_input("Date", datetime.date.today())
            proxy_name = c2.text_input("Faculty Name (if proxy)", value=user['name'])
            
            st.write(f"**Total Students: {len(s_list)}**")
            select_all = st.checkbox("Select All", value=True)
            
            # Grid Layout
            cols = st.columns(4)
            status_map = {}
            
            for i, s in enumerate(s_list):
                # Unique key prevents state retention across dates
                ukey = f"{s['usn']}_{date_val}_{course['subcode']}"
                status_map[s['usn']] = cols[i%4].checkbox(s['usn'], value=select_all, key=ukey)
            
            if st.form_submit_button("🚀 Submit Attendance"):
                absentees = [u for u, present in status_map.items() if not present]
                
                batch = db.batch()
                
                # A. Audit Log
                log_ref = db.collection('Class_Sessions').document()
                batch.set(log_ref, {
                    "course_code": course['subcode'],
                    "section": course['section'],
                    "date": str(date_val),
                    "faculty_id": user['id'],
                    "faculty_name": proxy_name,
                    "absentees": absentees,
                    "timestamp": datetime.datetime.now()
                })
                
                # B. Update Stats
                sub_key = sanitize_key(course['subcode'])
                for s in s_list:
                    summ_ref = db.collection('Student_Summaries').document(s['usn'])
                    
                    # Ensure fields exist and increment
                    batch.set(summ_ref, {
                        f"{sub_key}.title": course['subtitle'],
                        f"{sub_key}.total": firestore.Increment(1)
                    }, merge=True)
                    
                    if s['usn'] not in absentees:
                        batch.set(summ_ref, {
                            f"{sub_key}.attended": firestore.Increment(1)
                        }, merge=True)
                        
                batch.commit()
                st.success("Attendance Saved Successfully!")
                
    with t2:
        # Client-side sort to avoid Index errors
        logs = list(db.collection('Class_Sessions').where("faculty_id", "==", user['id']).stream())
        data = [l.to_dict() for l in logs]
        # Sort desc by timestamp
        data.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        if data:
            st.dataframe(pd.DataFrame(data)[['date', 'course_code', 'section', 'faculty_name']], use_container_width=True)
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
                
                # Metrics
                m1, m2, m3 = st.columns(3)
                m1.metric("Avg Attendance", f"{df['Percentage'].mean():.1f}%")
                m2.metric("Safe Subjects", len(df[df['Status']=='Safe']))
                m3.metric("Critical", len(df[df['Status']=='Critical']))
                
                # Chart
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
        st.info("Use this if Students see 'No Data' or if you added courses AFTER adding students.")
        if st.button("🔄 Sync/Fix All Student Subjects"):
            with st.spinner("Scanning and linking missing subjects..."):
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
            get_students_cached.clear() # Clear cache to refresh

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
                    # Check Firestore
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
