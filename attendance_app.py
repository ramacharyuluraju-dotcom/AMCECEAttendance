import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore, storage
import datetime
import altair as alt

# ==========================================
# 1. ROBUST SETUP & INIT
# ==========================================

st.set_page_config(page_title="VTU Attendance Pro", layout="wide", page_icon="🎓")

# Initialize Firebase (Error-Proof)
if not firebase_admin._apps:
    try:
        # CLOUD: Try loading from Secrets
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
    except Exception:
        # LOCAL: Fallback to file (Must be in same folder)
        try:
            cred = credentials.Certificate("firebase_key.json")
        except:
            st.error("🚨 CRITICAL: No credentials found. Add 'firebase_key.json' locally or 'secrets' on Cloud.")
            st.stop()
            
    # REPLACE with your actual bucket name
    BUCKET_NAME = "your-project-id.appspot.com" 
    
    firebase_admin.initialize_app(cred, {
        'storageBucket': BUCKET_NAME
    })

db = firestore.client()

# Session State for Persistent Login
if 'user' not in st.session_state:
    st.session_state['user'] = None

# ==========================================
# 2. CORE LOGIC (Database & CSV)
# ==========================================

def clean_csv_headers(df):
    """Normalize headers to handle typos (e.g., 'Sub Code' vs 'SubCode')"""
    df.columns = [c.strip().lower().replace(" ", "") for c in df.columns]
    return df

def batch_process_part_a(df):
    """
    PART A: Course Allocation & Faculty Login
    Expected Cols: ay, dept, sem, section, subcode, subtitle, facultyname, facultyemail
    """
    df = clean_csv_headers(df)
    batch = db.batch()
    count = 0
    
    for _, row in df.iterrows():
        # Create Unique Course ID: 2024_CS_5_A_18CS51
        cid = f"{row['ay']}_{row['dept']}_{row['sem']}_{row['section']}_{row['subcode']}"
        
        # 1. Save Course
        course_ref = db.collection('Courses').document(cid)
        batch.set(course_ref, {
            "ay": str(row['ay']),
            "dept": row['dept'].upper(),
            "sem": str(row['sem']),
            "section": row['section'].upper(),
            "subcode": row['subcode'].upper(),
            "subtitle": row['subtitle'],
            "faculty_id": row['facultyemail'].lower().strip(),
            "faculty_name": row['facultyname'] # Default faculty
        })
        
        # 2. Create Faculty Login (if missing)
        fid = row['facultyemail'].lower().strip()
        user_ref = db.collection('Users').document(fid)
        if not user_ref.get().exists:
            batch.set(user_ref, {
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

def batch_process_part_b(df):
    """
    PART B: Student Registration & Subject Linking
    Expected Cols: usn, name, dept, sem, section
    """
    df = clean_csv_headers(df)
    batch = db.batch()
    count = 0
    
    for _, row in df.iterrows():
        usn = str(row['usn']).strip().upper()
        dept = row['dept'].upper()
        sem = str(row['sem'])
        sec = row['section'].upper()
        
        # 1. Save Student Profile
        stu_ref = db.collection('Students').document(usn)
        batch.set(stu_ref, {
            "name": row['name'],
            "dept": dept,
            "sem": sem,
            "section": sec
        })
        
        # 2. Create Student Login
        user_ref = db.collection('Users').document(usn)
        if not user_ref.get().exists:
            batch.set(user_ref, {
                "name": row['name'],
                "password": usn, # Default Pwd = USN
                "role": "Student"
            })
            
        # 3. AUTO-LINKING: Initialize Summary for subjects in this Section
        # Find all courses for this Dept/Sem/Sec
        # Note: This query runs for every student (can be slow in bulk). 
        # Optimized: In production, fetch courses ONCE outside loop.
        courses = db.collection('Courses')\
            .where("dept", "==", dept)\
            .where("sem", "==", sem)\
            .where("section", "==", sec)\
            .stream()
            
        for c in courses:
            c_data = c.to_dict()
            sub_code = c_data['subcode']
            
            # Initialize Student Summary for this subject if not exists
            summ_ref = db.collection('Student_Summaries').document(usn)
            batch.set(summ_ref, {
                sub_code: {
                    "total": 0, 
                    "attended": 0, 
                    "title": c_data['subtitle']
                }
            }, merge=True)

        count += 1
        if count % 200 == 0: # Lower batch size due to extra writes
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

# ==========================================
# 3. ADMIN DASHBOARD
# ==========================================

def admin_dashboard():
    st.title("🛠 Admin Control Panel")
    
    tab1, tab2 = st.tabs(["📤 Bulk Upload (CSV)", "📝 Modify Data"])
    
    with tab1:
        col1, col2 = st.columns(2)
        
        # --- PART A ---
        with col1:
            st.subheader("Part A: Academics")
            st.info("Cols: AY, Dept, Sem, Section, SubCode, SubTitle, FacultyName, FacultyEmail")
            f1 = st.file_uploader("Upload Courses.csv", type='csv')
            if f1 and st.button("Process Part A"):
                with st.spinner("Creating Courses..."):
                    try:
                        c = batch_process_part_a(pd.read_csv(f1))
                        st.success(f"✅ Created {c} Course mappings.")
                    except Exception as e:
                        st.error(f"CSV Error: {e}")

        # --- PART B ---
        with col2:
            st.subheader("Part B: Students")
            st.info("Cols: USN, Name, Dept, Sem, Section")
            f2 = st.file_uploader("Upload Students.csv", type='csv')
            if f2 and st.button("Process Part B"):
                with st.spinner("Registering & Linking Subjects..."):
                    try:
                        c = batch_process_part_b(pd.read_csv(f2))
                        st.success(f"✅ Registered {c} Students.")
                    except Exception as e:
                        st.error(f"CSV Error: {e}")

    with tab2:
        st.write("### Data Tools")
        # Quick Faculty Creator
        with st.expander("Create Single Faculty Login"):
            em = st.text_input("Email").strip()
            nm = st.text_input("Name")
            if st.button("Create Faculty"):
                db.collection('Users').document(em).set({
                    "name": nm, "password": "password123", "role": "Faculty"
                })
                st.success("Created!")

# ==========================================
# 4. FACULTY DASHBOARD
# ==========================================

def faculty_dashboard(user):
    st.header(f"👨‍🏫 Welcome, {user['name']}")
    
    # 1. Get My Courses
    courses = list(db.collection('Courses').where("faculty_id", "==", user['id']).stream())
    
    if not courses:
        st.warning("⚠️ No courses assigned. Ask Admin to upload Part A.")
        return

    # Dropdown format: "18CS51 - DBMS (Sec A)"
    c_map = {f"{d.to_dict()['subcode']} - {d.to_dict()['subtitle']} ({d.to_dict()['section']})": d.to_dict() for d in courses}
    sel_name = st.selectbox("Select Class to Mark", list(c_map.keys()))
    sel_course = c_map[sel_name]
    
    st.divider()
    
    # 2. Get Students (Strict Filter)
    # Using simple 'where' filters (No FieldFilter import)
    students = db.collection('Students')\
        .where("dept", "==", sel_course['dept'])\
        .where("sem", "==", sel_course['sem'])\
        .where("section", "==", sel_course['section'])\
        .stream()
        
    student_list = sorted([{"usn": d.id, "name": d.to_dict()['name']} for d in students], key=lambda x: x['usn'])
    
    if not student_list:
        st.error("No students found in this section. Ask Admin to upload Part B.")
        return

    # 3. Attendance Form
    with st.form("att_screen"):
        c1, c2 = st.columns([1, 2])
        date_val = c1.date_input("Class Date", datetime.date.today())
        
        # EDITABLE FACULTY NAME (Substitute Handling)
        fac_name = c2.text_input("Faculty Taking Class", value=sel_course['faculty_name'])
        
        st.markdown("### 📝 Student List")
        status = {}
        
        # Responsive Grid
        cols = st.columns(3)
        for i, s in enumerate(student_list):
            col = cols[i % 3]
            status[s['usn']] = col.checkbox(f"{s['usn']}", value=True) # Default Present
            
        if st.form_submit_button("💾 Save Attendance"):
            absentees = [u for u, p in status.items() if not p]
            
            batch = db.batch()
            
            # A. Audit Log
            log_ref = db.collection('Class_Sessions').document()
            batch.set(log_ref, {
                "course_code": sel_course['subcode'],
                "section": sel_course['section'],
                "date": str(date_val),
                "faculty_name": fac_name, # Saved name
                "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            # B. Update Stats
            for s in student_list:
                summ_ref = db.collection('Student_Summaries').document(s['usn'])
                key = sel_course['subcode']
                
                # Use Set with Merge to ensure fields exist
                upd = {
                    f"{key}.total": firestore.Increment(1),
                    f"{key}.title": sel_course['subtitle']
                }
                if s['usn'] not in absentees:
                    upd[f"{key}.attended"] = firestore.Increment(1)
                    
                batch.set(summ_ref, upd, merge=True)
                
            batch.commit()
            st.success("Attendance Saved Successfully!")

# ==========================================
# 5. STUDENT DASHBOARD (Visual)
# ==========================================

def student_dashboard(user):
    st.header(f"🎓 Attendance Report: {user['id']}")
    
    # Fetch Data
    doc = db.collection('Student_Summaries').document(user['id']).get()
    
    if not doc.exists:
        st.info("No attendance records found.")
        return
        
    data = doc.to_dict()
    rows = []
    
    for sub_code, stats in data.items():
        if isinstance(stats, dict) and 'total' in stats:
            tot = stats['total']
            att = stats.get('attended', 0)
            pct = (att / tot * 100) if tot > 0 else 0
            
            status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
            
            rows.append({
                "Subject": sub_code,
                "Title": stats.get('title', sub_code),
                "Percentage": pct,
                "Status": status,
                "Classes": f"{att}/{tot}"
            })
            
    if rows:
        df = pd.DataFrame(rows)
        
        # 1. ALTAIR CHART (Red/Green Visuals)
        st.subheader("Visual Status")
        
        chart = alt.Chart(df).mark_bar().encode(
            x=alt.X('Subject', sort=None),
            y=alt.Y('Percentage', scale=alt.Scale(domain=[0, 100])),
            color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None),
            tooltip=['Title', 'Percentage', 'Classes', 'Status']
        ).properties(height=300)
        
        # Add 75% and 85% Rule Lines
        rule75 = alt.Chart(pd.DataFrame({'y': [75]})).mark_rule(color='red', strokeDash=[5,5]).encode(y='y')
        rule85 = alt.Chart(pd.DataFrame({'y': [85]})).mark_rule(color='green', strokeDash=[5,5]).encode(y='y')
        
        st.altair_chart(chart + rule75 + rule85, use_container_width=True)
        
        # 2. Detail Cards
        st.subheader("Details")
        for _, row in df.iterrows():
            with st.container():
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.write(f"**{row['Title']}**")
                c2.write(f"{row['Classes']}")
                color = "green" if row['Percentage'] >= 85 else "red"
                c3.markdown(f":{color}[{row['Percentage']:.1f}%]")
                st.divider()
    else:
        st.warning("No subject data available.")

# ==========================================
# 6. AUTH & ROUTING
# ==========================================

def main():
    if st.session_state['user'] is None:
        c1, c2 = st.columns([1, 2])
        with c1:
            st.title("🔐 Login")
            uid = st.text_input("User ID / USN").strip()
            pwd = st.text_input("Password", type="password").strip()
            
            if st.button("Sign In"):
                if uid == "admin" and pwd == "admin123":
                    st.session_state['user'] = {"id": "admin", "name": "Admin", "role": "Admin"}
                    st.rerun()
                else:
                    # Check Firestore
                    u_doc = db.collection('Users').document(uid).get()
                    if u_doc.exists and u_doc.to_dict().get('password') == pwd:
                        st.session_state['user'] = u_doc.to_dict()
                        st.session_state['user']['id'] = uid # Ensure ID is set
                        st.rerun()
                    else:
                        st.error("Invalid Credentials")
    else:
        user = st.session_state['user']
        
        with st.sidebar:
            st.write(f"Logged in: **{user['name']}**")
            st.write(f"Role: **{user['role']}**")
            if st.button("Logout"):
                st.session_state['user'] = None
                st.rerun()
                
        if user['role'] == "Admin":
            admin_dashboard()
        elif user['role'] == "Faculty":
            faculty_dashboard(user)
        elif user['role'] == "Student":
            student_dashboard(user)

if __name__ == "__main__":
    main()
