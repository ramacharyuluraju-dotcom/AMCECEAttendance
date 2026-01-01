import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore, storage
import datetime
import altair as alt

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

# Session State for Faculty/Admin Login only
if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# ==========================================
# 2. CORE FUNCTIONS
# ==========================================

def clean_headers(df):
    """Standardize CSV headers"""
    df.columns = [c.strip().lower().replace(" ", "") for c in df.columns]
    return df

def batch_process_courses(df):
    """Part A: Upload Courses & Create Faculty Logins"""
    df = clean_headers(df)
    batch = db.batch()
    count = 0
    
    for _, row in df.iterrows():
        # ID: AY_Dept_Sem_Sec_SubCode
        cid = f"{row['ay']}_{row['dept']}_{row['sem']}_{row['section']}_{row['subcode']}"
        
        batch.set(db.collection('Courses').document(cid), {
            "ay": str(row['ay']),
            "dept": row['dept'].upper(),
            "sem": str(row['sem']),
            "section": row['section'].upper(),
            "subcode": row['subcode'].upper(),
            "subtitle": row['subtitle'],
            "faculty_id": row['facultyemail'].lower().strip(),
            "faculty_name": row['facultyname']
        })
        
        # Create Faculty Login
        fid = row['facultyemail'].lower().strip()
        if not db.collection('Users').document(fid).get().exists:
            batch.set(db.collection('Users').document(fid), {
                "name": row['facultyname'],
                "password": "password123",
                "role": "Faculty",
                "dept": row['dept'].upper()
            })
            
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    return count

def batch_process_students(df):
    """Part B: Upload Students & Link Subjects"""
    df = clean_headers(df)
    batch = db.batch()
    count = 0
    
    for _, row in df.iterrows():
        usn = str(row['usn']).strip().upper()
        dept = row['dept'].upper()
        sem = str(row['sem'])
        sec = row['section'].upper()
        
        # 1. Save Profile
        batch.set(db.collection('Students').document(usn), {
            "name": row['name'],
            "dept": dept, "sem": sem, "section": sec
        })
        
        # 2. Auto-Link Subjects: Find courses for this section
        courses = db.collection('Courses')\
            .where("dept", "==", dept)\
            .where("sem", "==", sem)\
            .where("section", "==", sec).stream()
            
        for c in courses:
            c_data = c.to_dict()
            # Initialize Summary
            summ_ref = db.collection('Student_Summaries').document(usn)
            batch.set(summ_ref, {
                c_data['subcode']: {
                    "total": 0, "attended": 0, "title": c_data['subtitle']
                }
            }, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    return count

# ==========================================
# 3. PUBLIC STUDENT VIEW (NO LOGIN)
# ==========================================

def student_public_view():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Attendance Portal</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center;'>Enter your USN to view your status.</p>", unsafe_allow_html=True)
    
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        usn_input = st.text_input("USN", placeholder="e.g. 1MV20CS001").strip().upper()
        check_btn = st.button("Check Attendance", use_container_width=True)
    
    if check_btn and usn_input:
        doc = db.collection('Student_Summaries').document(usn_input).get()
        
        if not doc.exists:
            st.error(f"USN {usn_input} not found in the system.")
            return
            
        data = doc.to_dict()
        rows = []
        
        # Prepare Data for Graph
        for sub, stats in data.items():
            if isinstance(stats, dict) and 'total' in stats:
                tot = stats['total']
                att = stats.get('attended', 0)
                pct = (att / tot * 100) if tot > 0 else 0
                status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
                
                rows.append({
                    "Subject": sub,
                    "Title": stats.get('title', sub),
                    "Percentage": pct,
                    "Status": status,
                    "Classes": f"{att}/{tot}"
                })
        
        if rows:
            df = pd.DataFrame(rows)
            st.divider()
            
            # --- METRICS ---
            m1, m2, m3 = st.columns(3)
            m1.metric("Total Subjects", len(df))
            m2.metric("Safe Subjects", len(df[df['Status']=='Safe']))
            m3.metric("Critical Risks", len(df[df['Status']=='Critical']))
            
            # --- ALTAIR CHART ---
            chart = alt.Chart(df).mark_bar().encode(
                x=alt.X('Subject', sort=None),
                y=alt.Y('Percentage', scale=alt.Scale(domain=[0, 100])),
                color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None),
                tooltip=['Title', 'Classes', 'Percentage']
            ).properties(height=350)
            
            rule75 = alt.Chart(pd.DataFrame({'y': [75]})).mark_rule(color='red', strokeDash=[3,3]).encode(y='y')
            rule85 = alt.Chart(pd.DataFrame({'y': [85]})).mark_rule(color='green', strokeDash=[3,3]).encode(y='y')
            
            st.altair_chart(chart + rule75 + rule85, use_container_width=True)
            
            # --- TABLE ---
            st.subheader("Detailed Breakdown")
            st.dataframe(df[['Subject', 'Title', 'Classes', 'Percentage', 'Status']], use_container_width=True)
        else:
            st.warning("No attendance data recorded yet.")

# ==========================================
# 4. FACULTY / ADMIN VIEWS (SECURE)
# ==========================================

def faculty_view(user):
    st.title(f"👨‍🏫 {user['name']}")
    
    # Get Courses
    courses = list(db.collection('Courses').where("faculty_id", "==", user['id']).stream())
    if not courses:
        st.warning("No courses assigned.")
        return

    c_map = {f"{d.to_dict()['subcode']} ({d.to_dict()['section']})": d.to_dict() for d in courses}
    sel_name = st.selectbox("Select Class", list(c_map.keys()))
    course = c_map[sel_name]
    
    st.divider()
    
    # Get Students
    students = db.collection('Students')\
        .where("dept", "==", course['dept'])\
        .where("sem", "==", course['sem'])\
        .where("section", "==", course['section']).stream()
    
    s_list = sorted([{"usn": d.id, "name": d.to_dict()['name']} for d in students], key=lambda x: x['usn'])
    
    if not s_list:
        st.error("No students linked to this section.")
        return

    with st.form("mark"):
        c1, c2 = st.columns([1, 2])
        dt = c1.date_input("Date", datetime.date.today())
        # EDITABLE NAME
        fname = c2.text_input("Faculty Taking Class", value=course['faculty_name'])
        
        st.write("### Roll Call")
        status = {}
        cols = st.columns(3)
        for i, s in enumerate(s_list):
            status[s['usn']] = cols[i%3].checkbox(f"{s['usn']}", value=True)
            
        if st.form_submit_button("Submit Attendance"):
            absentees = [u for u, p in status.items() if not p]
            batch = db.batch()
            
            # Log
            batch.set(db.collection('Class_Sessions').document(), {
                "course_code": course['subcode'],
                "section": course['section'],
                "date": str(dt),
                "faculty_name": fname,
                "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            # Update Stats
            for s in s_list:
                ref = db.collection('Student_Summaries').document(s['usn'])
                key = course['subcode']
                upd = {f"{key}.total": firestore.Increment(1), f"{key}.title": course['subtitle']}
                if s['usn'] not in absentees:
                    upd[f"{key}.attended"] = firestore.Increment(1)
                batch.set(ref, upd, merge=True)
                
            batch.commit()
            st.success("Saved!")

def admin_view():
    st.title("⚙️ Admin Panel")
    t1, t2 = st.tabs(["Part A: Courses", "Part B: Students"])
    
    with t1:
        st.info("CSV: AY, Dept, Sem, Section, SubCode, SubTitle, FacultyName, FacultyEmail")
        f = st.file_uploader("Upload Courses", type='csv', key='a')
        if f and st.button("Process A"):
            c = batch_process_courses(pd.read_csv(f))
            st.success(f"{c} Courses Created.")
            
    with t2:
        st.info("CSV: USN, Name, Dept, Sem, Section")
        f = st.file_uploader("Upload Students", type='csv', key='b')
        if f and st.button("Process B"):
            c = batch_process_students(pd.read_csv(f))
            st.success(f"{c} Students Registered.")

# ==========================================
# 5. MAIN NAVIGATION
# ==========================================

def main():
    # Sidebar for Secure Login
    with st.sidebar:
        st.title("Staff Login")
        if st.session_state['auth_user']:
            st.success(f"Hi, {st.session_state['auth_user']['name']}")
            if st.button("Logout"):
                st.session_state['auth_user'] = None
                st.rerun()
        else:
            uid = st.text_input("Email/AdminID")
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

    # Main Area Logic
    user = st.session_state['auth_user']
    
    if user:
        if user['role'] == 'Admin':
            admin_view()
        else:
            faculty_view(user)
    else:
        # DEFAULT SCREEN: PUBLIC STUDENT VIEW
        student_public_view()

if __name__ == "__main__":
    main()
