import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore, storage
import datetime
import time

# ==========================================
# 1. SETUP & AUTHENTICATION
# ==========================================

st.set_page_config(page_title="VTU Attendance System", layout="wide")

# Initialize Firebase
if not firebase_admin._apps:
    try:
        # CLOUD: Try loading from Secrets
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
    except Exception:
        # LOCAL: Fallback to file
        cred = credentials.Certificate("firebase_key.json")
        
    # REPLACE with your bucket name
    BUCKET_NAME = "your-project-id.appspot.com" 
    
    firebase_admin.initialize_app(cred, {
        'storageBucket': BUCKET_NAME
    })

db = firestore.client()
bucket = storage.bucket()

# Initialize Session State
if 'user' not in st.session_state:
    st.session_state['user'] = None
if 'role' not in st.session_state:
    st.session_state['role'] = None

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================

def login(username, password):
    """Simple Auth System"""
    # 1. Hardcoded Admin
    if username == "admin" and password == "admin123":
        return {"name": "Administrator", "role": "Admin", "id": "admin"}
    
    # 2. Check Firestore Users (Faculty/Students)
    # Users collection structure: { "password": "...", "role": "Faculty", "name": "..." }
    doc = db.collection('Users').document(username).get()
    if doc.exists:
        user_data = doc.to_dict()
        if user_data.get('password') == password:
            user_data['id'] = username
            return user_data
    return None

def batch_write_courses(df):
    """Part A: Upload Courses & Create Faculty Logins"""
    batch = db.batch()
    count = 0
    
    for index, row in df.iterrows():
        # 1. Create Course Document
        # ID format: AY_Dept_Sem_Sec_SubCode (e.g., 2023_CS_5_A_18CS51)
        course_id = f"{row['AY']}_{row['Dept']}_{row['Sem']}_{row['Section']}_{row['SubCode']}"
        course_ref = db.collection('Courses').document(course_id)
        
        course_data = {
            "ay": str(row['AY']),
            "dept": row['Dept'],
            "sem": str(row['Sem']),
            "section": row['Section'],
            "subcode": row['SubCode'],
            "subtitle": row['SubTitle'],
            "faculty_id": row['FacultyEmail'], # Link to faculty
            "faculty_name": row['FacultyName']
        }
        batch.set(course_ref, course_data)
        
        # 2. Create Faculty Login (if not exists)
        user_ref = db.collection('Users').document(row['FacultyEmail'])
        if not user_ref.get().exists:
            batch.set(user_ref, {
                "name": row['FacultyName'],
                "password": "password123", # Default Password
                "role": "Faculty",
                "dept": row['Dept']
            })
        
        count += 1
        if count % 400 == 0: # Firestore batch limit is 500
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

def batch_write_students(df):
    """Part B: Upload Student List"""
    batch = db.batch()
    count = 0
    
    for index, row in df.iterrows():
        usn = str(row['USN']).upper().strip()
        student_ref = db.collection('Students').document(usn)
        
        student_data = {
            "name": row['Name'],
            "dept": row['Dept'],
            "sem": str(row['Sem']),
            "section": row['Section'],
            "email": row.get('Email', f"{usn}@college.edu")
        }
        batch.set(student_ref, student_data)
        
        # Create Student Login
        user_ref = db.collection('Users').document(usn)
        if not user_ref.get().exists:
            batch.set(user_ref, {
                "name": row['Name'],
                "password": usn, # Default pass is USN
                "role": "Student"
            })

        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

# ==========================================
# 3. ADMIN VIEW
# ==========================================

def admin_view():
    st.title("Admin Control Center")
    
    tab1, tab2, tab3 = st.tabs(["Part A: Course & Faculty", "Part B: Student Bulk Load", "Modify Data"])
    
    # --- PART A: LOAD ACADEMICS ---
    with tab1:
        st.subheader("📁 Upload Course Allocation (Part A)")
        st.info("CSV Columns Required: AY, Dept, Sem, Section, SubCode, SubTitle, FacultyName, FacultyEmail")
        
        uploaded_file = st.file_uploader("Upload Courses CSV", type=['csv'], key="part_a")
        if uploaded_file:
            df = pd.read_csv(uploaded_file)
            st.dataframe(df.head())
            if st.button("Process Part A"):
                with st.spinner("Creating Courses & Faculty Logins..."):
                    count = batch_write_courses(df)
                st.success(f"Successfully processed {count} records!")

    # --- PART B: LOAD STUDENTS ---
    with tab2:
        st.subheader("👨‍🎓 Upload Student List (Part B)")
        st.info("CSV Columns Required: USN, Name, Dept, Sem, Section")
        
        uploaded_file_b = st.file_uploader("Upload Students CSV", type=['csv'], key="part_b")
        if uploaded_file_b:
            df_b = pd.read_csv(uploaded_file_b)
            st.dataframe(df_b.head())
            if st.button("Process Part B"):
                with st.spinner("Registering Students..."):
                    count = batch_write_students(df_b)
                st.success(f"Successfully registered {count} students!")

    # --- MODIFY DATA ---
    with tab3:
        st.subheader("✏️ Modify Records")
        edit_type = st.radio("What to edit?", ["Student", "Course/Faculty"])
        
        if edit_type == "Student":
            search_usn = st.text_input("Enter USN to Edit").upper()
            if search_usn and st.button("Search Student"):
                doc_ref = db.collection('Students').document(search_usn)
                doc = doc_ref.get()
                if doc.exists:
                    data = doc.to_dict()
                    with st.form("edit_student"):
                        new_name = st.text_input("Name", data.get('name'))
                        new_sec = st.text_input("Section", data.get('section'))
                        if st.form_submit_button("Update Student"):
                            doc_ref.update({"name": new_name, "section": new_sec})
                            st.success("Updated!")
                else:
                    st.error("Student not found.")
                    
        elif edit_type == "Course/Faculty":
            # Simple manual fetch for now
            search_code = st.text_input("Enter SubCode (e.g., 18CS51)")
            if search_code and st.button("Find Courses"):
                docs = db.collection('Courses').where("subcode", "==", search_code).stream()
                for doc in docs:
                    d = doc.to_dict()
                    st.write(f"Found: {d['ay']} - {d['section']} - {d['faculty_name']}")
                    if st.button(f"Delete {doc.id}", key=doc.id):
                        db.collection('Courses').document(doc.id).delete()
                        st.warning("Deleted. Refresh to see changes.")

# ==========================================
# 4. FACULTY VIEW (Dynamic)
# ==========================================

def faculty_view(user_email):
    st.header(f"👨‍🏫 Welcome, {st.session_state['user']['name']}")
    
    # 1. Fetch Assigned Courses
    courses_ref = db.collection('Courses').where("faculty_id", "==", user_email).stream()
    my_courses = [doc.to_dict() for doc in courses_ref]
    
    if not my_courses:
        st.warning("No courses assigned to you in the database.")
        return

    # 2. Select Course
    course_options = {f"{c['subcode']} ({c['section']})": c for c in my_courses}
    selected_label = st.selectbox("Select Class", list(course_options.keys()))
    selected_course = course_options[selected_label]
    
    st.info(f"Marking Attendance for: {selected_course['subtitle']} - Sec {selected_course['section']}")
    
    date_sel = st.date_input("Date", datetime.date.today())
    
    # 3. Fetch Students for this Section
    students_ref = db.collection('Students')\
        .where("dept", "==", selected_course['dept'])\
        .where("sem", "==", selected_course['sem'])\
        .where("section", "==", selected_course['section'])\
        .stream()
        
    student_list = [{"usn": doc.id, "name": doc.to_dict()['name']} for doc in students_ref]
    student_list.sort(key=lambda x: x['usn']) # Sort by USN
    
    if not student_list:
        st.error("No students found for this class section. Please ask Admin to upload Part B.")
        return

    # 4. Marking Interface
    with st.form("att_form"):
        status = {}
        cols = st.columns(3)
        for i, stu in enumerate(student_list):
            col = cols[i % 3]
            # Default True (Present)
            status[stu['usn']] = col.checkbox(f"{stu['usn']}", value=True)
            
        if st.form_submit_button("Submit Attendance"):
            absentees = [usn for usn, present in status.items() if not present]
            
            # Batch Write Logic
            batch = db.batch()
            
            # A. Session Log
            sess_ref = db.collection('Class_Sessions').document()
            batch.set(sess_ref, {
                "date": str(date_sel),
                "course_code": selected_course['subcode'],
                "section": selected_course['section'],
                "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            # B. Student Summaries
            course_key = selected_course['subcode'] # e.g., 18CS51
            for stu in student_list:
                usn = stu['usn']
                summ_ref = db.collection('Student_Summaries').document(usn)
                
                upd = {
                    f"{course_key}.total": firestore.Increment(1),
                    f"{course_key}.title": selected_course['subtitle']
                }
                if usn not in absentees:
                    upd[f"{course_key}.attended"] = firestore.Increment(1)
                
                batch.set(summ_ref, upd, merge=True)
                
            batch.commit()
            st.success("Attendance Recorded Successfully!")

# ==========================================
# 5. STUDENT VIEW (Read Only)
# ==========================================

def student_view(usn):
    st.header(f"🎓 Student Dashboard: {usn}")
    
    doc = db.collection('Student_Summaries').document(usn).get()
    
    if doc.exists:
        data = doc.to_dict()
        st.subheader("Attendance Report")
        
        for code, info in data.items():
            if isinstance(info, dict) and 'total' in info:
                total = info['total']
                attended = info.get('attended', 0)
                pct = (attended / total * 100) if total > 0 else 0
                
                # VTU Logic (85% Green, 75% Orange, <75% Red)
                if pct >= 85:
                    color = "green"
                elif pct >= 75:
                    color = "orange"
                else:
                    color = "red"
                    
                st.markdown(f"**{info.get('title', code)}**")
                st.markdown(f":{color}[{pct:.1f}%] ({attended}/{total})")
                st.progress(pct / 100)
                st.divider()
    else:
        st.info("No attendance data found.")

# ==========================================
# 6. LOGIN & MAIN FLOW
# ==========================================

def main():
    # If not logged in, show Login Screen
    if st.session_state['user'] is None:
        st.title("🔐 VTU System Login")
        
        col1, col2 = st.columns([1, 2])
        with col1:
            username = st.text_input("User ID / Email")
            password = st.text_input("Password", type="password")
            
            if st.button("Login"):
                user = login(username, password)
                if user:
                    st.session_state['user'] = user
                    st.session_state['role'] = user['role']
                    st.rerun()
                else:
                    st.error("Invalid Credentials")
        
        with col2:
            st.info("Default Admin: admin / admin123")
            
    else:
        # LOGGED IN
        user = st.session_state['user']
        
        # Sidebar
        st.sidebar.write(f"Logged in as: **{user['name']}** ({user['role']})")
        if st.sidebar.button("Logout"):
            st.session_state['user'] = None
            st.rerun()
            
        # Routing
        if user['role'] == "Admin":
            admin_view()
        elif user['role'] == "Faculty":
            faculty_view(user['id']) # Pass email
        elif user['role'] == "Student":
            student_view(user['id']) # Pass USN

if __name__ == "__main__":
    main()
