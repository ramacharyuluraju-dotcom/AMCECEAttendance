import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore, storage
import datetime
import altair as alt
import io

# ==========================================
# 1. SETUP
# ==========================================

st.set_page_config(page_title="VTU Attendance", page_icon="🎓", layout="wide")

# Initialize Firebase
if not firebase_admin._apps:
    try:
        # Cloud Secrets
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
    except:
        # Local File
        try:
            cred = credentials.Certificate("firebase_key.json")
        except:
            st.error("Missing firebase_key.json or Secrets.")
            st.stop()
            
    BUCKET_NAME = "your-project-id.appspot.com" 
    firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})

db = firestore.client()

if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# ==========================================
# 2. CORE FUNCTIONS
# ==========================================

def standardize_columns(df, required_cols):
    """
    Tries to map uploaded columns to required standard keys.
    Returns: (Renamed DataFrame, List of Missing Columns)
    """
    # Normalize current headers: lowercase, remove spaces/special chars
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    # Map common variations to standard keys
    # Key = Standard, Value = List of possible variations found in normalized headers
    mapping = {}
    missing = []
    
    for req in required_cols:
        if req in df.columns:
            continue # Already exists
        else:
            missing.append(req)
            
    return df, missing

def batch_process_courses(df):
    """Part A: Upload Courses"""
    # 1. Normalize Headers
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    # 2. Check for missing columns
    required = ['ay', 'dept', 'sem', 'section', 'subcode', 'subtitle', 'facultyname', 'facultyemail']
    missing = [col for col in required if col not in df.columns]
    
    if missing:
        st.error(f"❌ CSV is missing columns: {', '.join(missing)}")
        st.info(f"Found headers: {list(df.columns)}")
        return 0

    df = df.fillna("")
    batch = db.batch()
    count = 0
    
    for _, row in df.iterrows():
        # Skip empty rows
        if not row['subcode'] or not row['facultyemail']:
            continue
            
        # Data Cleaning
        ay = str(row['ay']).strip()
        dept = str(row['dept']).strip().upper()
        sem = str(row['sem']).strip()
        section = str(row['section']).strip().upper()
        subcode = str(row['subcode']).strip().upper()
        subtitle = str(row['subtitle']).strip()
        fac_email = str(row['facultyemail']).strip().lower()
        fac_name = str(row['facultyname']).strip()

        # ID: AY_Dept_Sem_Sec_SubCode
        cid = f"{ay}_{dept}_{sem}_{section}_{subcode}"
        
        # Save Course
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode, "subtitle": subtitle,
            "faculty_id": fac_email, "faculty_name": fac_name
        })
        
        # Create Faculty Login
        if fac_email:
            u_ref = db.collection('Users').document(fac_email)
            if not u_ref.get().exists:
                batch.set(u_ref, {
                    "name": fac_name, "password": "password123", "role": "Faculty", "dept": dept
                })
            
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

def batch_process_students(df):
    """Part B: Upload Students"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    required = ['usn', 'name', 'dept', 'sem', 'section']
    missing = [col for col in required if col not in df.columns]
    
    if missing:
        st.error(f"❌ CSV is missing columns: {', '.join(missing)}")
        return 0

    df = df.fillna("")
    batch = db.batch()
    count = 0
    
    # Pre-fetch courses to optimize speed (avoid querying inside loop)
    # We group courses by "Dept_Sem_Sec" key
    all_courses = db.collection('Courses').stream()
    course_map = {} # Key: "CS_5_A", Value: List of subcodes/titles
    
    for c in all_courses:
        d = c.to_dict()
        key = f"{d['dept']}_{d['sem']}_{d['section']}"
        if key not in course_map: course_map[key] = []
        course_map[key].append({"code": d['subcode'], "title": d['subtitle']})
    
    for _, row in df.iterrows():
        if not row['usn']: continue
            
        usn = str(row['usn']).strip().upper()
        dept = str(row['dept']).strip().upper()
        sem = str(row['sem']).strip()
        sec = str(row['section']).strip().upper()
        name = str(row['name']).strip()
        
        # 1. Save Profile
        batch.set(db.collection('Students').document(usn), {
            "name": name, "dept": dept, "sem": sem, "section": sec
        })
        
        # 2. Auto-Link Subjects from Memory Map
        key = f"{dept}_{sem}_{sec}"
        if key in course_map:
            summ_ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for subj in course_map[key]:
                # Prepare update: "18CS51.total": 0 (using Merge to not overwrite existing attendance)
                updates[f"{subj['code']}.total"] = 0
                updates[f"{subj['code']}.attended"] = 0
                updates[f"{subj['code']}.title"] = subj['title']
            
            # Note: We must check if doc exists to determine merge strategy, 
            # but here we use simple merge. 
            # Ideally, we only set initial values if keys don't exist, 
            # but Firestore merge will overwrite 0.
            # FIX: Only set title/total=0 if we want to reset. 
            # For "Add New Student", this is fine.
            batch.set(summ_ref, updates, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

# ==========================================
# 3. VIEWS
# ==========================================

def student_public_view():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Portal</h1>", unsafe_allow_html=True)
    
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        usn_input = st.text_input("Enter USN", placeholder="1MV20CS001").strip().upper()
        if st.button("Check Attendance", use_container_width=True):
            if not usn_input: return
            
            doc = db.collection('Student_Summaries').document(usn_input).get()
            if not doc.exists:
                st.error("USN not found.")
                return
                
            data = doc.to_dict()
            rows = []
            for sub, stats in data.items():
                if isinstance(stats, dict) and 'total' in stats:
                    tot = stats['total']
                    att = stats.get('attended', 0)
                    pct = (att / tot * 100) if tot > 0 else 0
                    status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
                    rows.append({"Subject": sub, "Title": stats.get('title', sub), "Percentage": pct, "Status": status, "Classes": f"{att}/{tot}"})
            
            if rows:
                df = pd.DataFrame(rows)
                st.divider()
                
                # Metrics
                c1, c2, c3 = st.columns(3)
                c1.metric("Average", f"{df['Percentage'].mean():.1f}%")
                c2.metric("Safe", len(df[df['Status']=='Safe']))
                c3.metric("Critical", len(df[df['Status']=='Critical']))
                
                # Chart
                base = alt.Chart(df).encode(x=alt.X('Subject', sort=None))
                bar = base.mark_bar().encode(
                    y=alt.Y('Percentage', scale=alt.Scale(domain=[0, 100])),
                    color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None),
                    tooltip=['Title', 'Classes', 'Percentage']
                )
                rule = base.mark_rule(color='red', strokeDash=[5,5]).encode(y=alt.datum(75))
                st.altair_chart(bar + rule, use_container_width=True)
            else:
                st.info("No attendance data linked.")

def faculty_view(user):
    st.title(f"👨‍🏫 {user['name']}")
    courses = list(db.collection('Courses').where("faculty_id", "==", user['id']).stream())
    
    if not courses:
        st.warning("No courses assigned.")
        return

    c_map = {f"{d.to_dict()['subcode']} ({d.to_dict()['section']})": d.to_dict() for d in courses}
    sel_name = st.selectbox("Select Class", list(c_map.keys()))
    course = c_map[sel_name]
    
    st.divider()
    
    students = db.collection('Students')\
        .where("dept", "==", course['dept'])\
        .where("sem", "==", course['sem'])\
        .where("section", "==", course['section']).stream()
    
    s_list = sorted([{"usn": d.id, "name": d.to_dict()['name']} for d in students], key=lambda x: x['usn'])
    
    if not s_list:
        st.error("No students found.")
        return

    with st.form("mark"):
        c1, c2 = st.columns([1, 2])
        dt = c1.date_input("Date", datetime.date.today())
        fname = c2.text_input("Faculty Name", value=course['faculty_name'])
        
        st.write(f"**Students: {len(s_list)}**")
        status = {}
        cols = st.columns(4)
        for i, s in enumerate(s_list):
            status[s['usn']] = cols[i%4].checkbox(f"{s['usn']}", value=True)
            
        if st.form_submit_button("Submit Attendance"):
            absentees = [u for u, p in status.items() if not p]
            batch = db.batch()
            
            batch.set(db.collection('Class_Sessions').document(), {
                "course_code": course['subcode'], "section": course['section'],
                "date": str(dt), "faculty_name": fname, "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            for s in s_list:
                ref = db.collection('Student_Summaries').document(s['usn'])
                key = course['subcode']
                # Increment total
                batch.set(ref, {f"{key}.total": firestore.Increment(1), f"{key}.title": course['subtitle']}, merge=True)
                # Increment attended if present
                if s['usn'] not in absentees:
                    batch.set(ref, {f"{key}.attended": firestore.Increment(1)}, merge=True)
                
            batch.commit()
            st.success("Saved!")

def admin_view():
    st.title("⚙️ Admin Panel")
    t1, t2 = st.tabs(["Part A: Courses", "Part B: Students"])
    
    # --- HELPER: Sample CSV Generator ---
    def get_csv_download(type_):
        if type_ == 'A':
            data = "AY,Dept,Sem,Section,SubCode,SubTitle,FacultyName,FacultyEmail\n2024,CS,5,A,18CS51,DBMS,John Doe,john@college.edu"
            name = "template_courses.csv"
        else:
            data = "USN,Name,Dept,Sem,Section\n1MV20CS001,Rahul,CS,5,A"
            name = "template_students.csv"
        return st.download_button(f"⬇️ Download {name}", data, name, "text/csv")

    with t1:
        c1, c2 = st.columns([3, 1])
        c1.info("Upload 'Courses' CSV. Headers must be exact.")
        with c2: get_csv_download('A')
            
        f = st.file_uploader("Upload Courses", type='csv', key='a')
        if f and st.button("Process A"):
            try:
                c = batch_process_courses(pd.read_csv(f))
                if c > 0: st.success(f"✅ {c} Courses Created.")
            except Exception as e:
                st.error(f"Error: {e}")
            
    with t2:
        c1, c2 = st.columns([3, 1])
        c1.info("Upload 'Students' CSV.")
        with c2: get_csv_download('B')
            
        f = st.file_uploader("Upload Students", type='csv', key='b')
        if f and st.button("Process B"):
            try:
                c = batch_process_students(pd.read_csv(f))
                if c > 0: st.success(f"✅ {c} Students Registered.")
            except Exception as e:
                st.error(f"Error: {e}")

# ==========================================
# 4. MAIN
# ==========================================

def main():
    with st.sidebar:
        st.title("Staff Login")
        if st.session_state['auth_user']:
            st.success(f"Hi, {st.session_state['auth_user']['name']}")
            if st.button("Logout"):
                st.session_state['auth_user'] = None
                st.rerun()
        else:
            uid = st.text_input("Email/ID")
            pwd = st.text_input("Password", type="password")
            if st.button("Login"):
                if uid == "admin" and pwd == "admin123":
                    st.session_state['auth_user'] = {"id": "admin", "name": "Admin", "role": "Admin"}
                    st.rerun()
                else:
                    doc = db.collection('Users').document(uid).get()
                    if doc.exists and doc.to_dict().get('password') == pwd:
                        u = doc.to_dict()
                        u['id'] = uid
                        st.session_state['auth_user'] = u
                        st.rerun()
                    else:
                        st.error("Invalid")

    user = st.session_state['auth_user']
    if user:
        if user['role'] == 'Admin': admin_view()
        else: faculty_view(user)
    else:
        student_public_view()

if __name__ == "__main__":
    main()
