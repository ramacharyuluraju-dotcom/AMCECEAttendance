import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter
import datetime

# ==========================================
# 1. CONFIGURATION
# ==========================================

st.set_page_config(page_title="VTU Attendance Visualizer", layout="wide")

if not firebase_admin._apps:
    try:
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
    except Exception:
        cred = credentials.Certificate("firebase_key.json")
        
    BUCKET_NAME = "your-project-id.appspot.com" 
    firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_NAME})

db = firestore.client()

if 'user' not in st.session_state:
    st.session_state['user'] = None

# ==========================================
# 2. LOGIN LOGIC
# ==========================================

def login_user(uid, pwd):
    if uid == "admin" and pwd == "admin123":
        return {"id": "admin", "name": "Administrator", "role": "Admin"}
    
    doc = db.collection('Users').document(uid).get()
    if doc.exists:
        data = doc.to_dict()
        if data.get('password') == pwd:
            data['id'] = uid
            return data
    return None

# ==========================================
# 3. STUDENT DASHBOARD (VISUAL ONLY)
# ==========================================

def student_visual_dashboard(user_data):
    st.header(f"📊 My Attendance Graph")
    usn = user_data['id']
    
    # 1. Fetch Summary Data
    doc = db.collection('Student_Summaries').document(usn).get()
    
    if not doc.exists:
        st.info("No attendance data available to visualize.")
        return

    data = doc.to_dict()
    chart_data = []
    
    # 2. Process Data for Graph
    for subject_code, stats in data.items():
        if isinstance(stats, dict) and 'total' in stats:
            total = stats['total']
            attended = stats.get('attended', 0)
            pct = (attended / total * 100) if total > 0 else 0
            
            # Color Logic (VTU Rules)
            # We can't color bars individually easily in simple st.bar_chart, 
            # so we prepare a dataframe for a custom chart or simple display.
            status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
            
            chart_data.append({
                "Subject": subject_code,
                "Attendance %": round(pct, 1),
                "Status": status,
                "Attended": attended,
                "Total": total
            })
    
    if chart_data:
        df = pd.DataFrame(chart_data)
        
        # 3. Display Metric Cards (Top Row)
        col1, col2, col3 = st.columns(3)
        avg_att = df["Attendance %"].mean()
        col1.metric("Average Attendance", f"{avg_att:.1f}%")
        col2.metric("Safe Subjects", len(df[df['Status'] == 'Safe']))
        col3.metric("Critical Subjects", len(df[df['Status'] == 'Critical']), delta_color="inverse")
        
        st.divider()
        
        # 4. Main Bar Chart
        st.subheader("Subject-wise Performance")
        # Using Streamlit's native simple bar chart
        st.bar_chart(df, x="Subject", y="Attendance %", color="#4F8BF9")
        
        # 5. Data Table (Optional, for detail)
        with st.expander("View Detailed Numbers"):
            st.dataframe(df.style.highlight_between(left=0, right=74.9, subset="Attendance %", color="#ffcccc"))
    else:
        st.warning("No subject data found.")

# ==========================================
# 4. FACULTY DASHBOARD (EDITABLE NAME)
# ==========================================

def faculty_marking_dashboard(user_data):
    st.header("📝 Mark Attendance")
    
    # 1. Fetch Assigned Courses
    courses = list(db.collection('Courses').where(filter=FieldFilter("faculty_id", "==", user_data['id'])).stream())
    
    if not courses:
        st.warning("No courses assigned.")
        return
        
    c_options = {f"{c.to_dict()['subcode']} ({c.to_dict()['section']})": c.to_dict() for c in courses}
    selected_name = st.selectbox("Select Class", list(c_options.keys()))
    course_data = c_options[selected_name]
    
    st.divider()
    
    # 2. Fetch Students (Mapped by Dept+Sem+Section)
    students_ref = db.collection('Students')\
        .where(filter=FieldFilter("dept", "==", course_data['dept']))\
        .where(filter=FieldFilter("sem", "==", course_data['sem']))\
        .where(filter=FieldFilter("section", "==", course_data['section']))\
        .stream()
        
    students = sorted([{"usn": doc.id, "name": doc.to_dict()['name']} for doc in students_ref], key=lambda x: x['usn'])
    
    if not students:
        st.error(f"No students found in {course_data['dept']} Sem {course_data['sem']} Sec {course_data['section']}")
        return

    # 3. Marking Form
    with st.form("mark_attendance"):
        c1, c2 = st.columns(2)
        date_val = c1.date_input("Date", datetime.date.today())
        
        # --- CRITICAL REQUIREMENT: EDITABLE FACULTY NAME ---
        # Prefilled with login name, but editable for substitutes
        faculty_name = c2.text_input("Faculty Taking Class", value=user_data['name'])
        
        st.markdown("### Student List")
        attendance_map = {}
        
        # Grid Layout
        cols = st.columns(3)
        for i, student in enumerate(students):
            col = cols[i % 3]
            attendance_map[student['usn']] = col.checkbox(f"{student['usn']}", value=True)
            
        if st.form_submit_button("Save Attendance Record"):
            absentees = [usn for usn, present in attendance_map.items() if not present]
            
            batch = db.batch()
            
            # A. Save Session (With Editable Faculty Name)
            session_ref = db.collection('Class_Sessions').document()
            batch.set(session_ref, {
                "course_code": course_data['subcode'],
                "section": course_data['section'],
                "date": str(date_val),
                "faculty_name": faculty_name, # <--- SAVES THE EDITED NAME
                "faculty_id_logged_in": user_data['id'], # Audit trail
                "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            # B. Update Aggregates
            for s in students:
                summ_ref = db.collection('Student_Summaries').document(s['usn'])
                key = course_data['subcode']
                
                updates = {
                    f"{key}.total": firestore.Increment(1),
                    f"{key}.title": course_data['subtitle']
                }
                if s['usn'] not in absentees:
                    updates[f"{key}.attended"] = firestore.Increment(1)
                    
                batch.set(summ_ref, updates, merge=True)
                
            batch.commit()
            st.success(f"Attendance marked by {faculty_name} for {len(students)} students!")

# ==========================================
# 5. ADMIN DASHBOARD (Simplified)
# ==========================================

def admin_dashboard():
    st.title("Admin Console")
    
    # Simple CSV Upload for "Part A" (Courses)
    uploaded_file = st.file_uploader("Upload Courses CSV (AY, Dept, Sem, Sec, SubCode, Title, FacName, FacEmail)")
    if uploaded_file and st.button("Process Courses"):
        df = pd.read_csv(uploaded_file)
        batch = db.batch()
        for _, row in df.iterrows():
            cid = f"{row['Dept']}_{row['Sem']}_{row['Section']}_{row['SubCode']}"
            ref = db.collection('Courses').document(cid)
            batch.set(ref, {
                "dept": row['Dept'], "sem": str(row['Sem']), "section": row['Section'],
                "subcode": row['SubCode'], "subtitle": row['SubTitle'],
                "faculty_id": row['FacultyEmail'], "faculty_name": row['FacultyName']
            })
            # Create Faculty Login
            u_ref = db.collection('Users').document(row['FacultyEmail'])
            if not u_ref.get().exists:
                batch.set(u_ref, {"name": row['FacultyName'], "password": "password123", "role": "Faculty"})
        batch.commit()
        st.success("Courses and Faculty Logins created.")
        
    # Simple CSV Upload for "Part B" (Students)
    st.divider()
    s_file = st.file_uploader("Upload Students CSV (USN, Name, Dept, Sem, Section)")
    if s_file and st.button("Process Students"):
        df = pd.read_csv(s_file)
        batch = db.batch()
        for _, row in df.iterrows():
            usn = str(row['USN']).upper().strip()
            # Profile
            batch.set(db.collection('Students').document(usn), {
                "name": row['Name'], "dept": row['Dept'], 
                "sem": str(row['Sem']), "section": row['Section']
            })
            # Login
            u_ref = db.collection('Users').document(usn)
            if not u_ref.get().exists:
                batch.set(u_ref, {"name": row['Name'], "password": usn, "role": "Student"})
        batch.commit()
        st.success("Students registered.")

# ==========================================
# 6. MAIN APP
# ==========================================

def main():
    if st.session_state['user'] is None:
        c1, c2 = st.columns([1,2])
        with c1:
            st.title("🔐 Login")
            uid = st.text_input("User ID")
            pwd = st.text_input("Password", type="password")
            if st.button("Sign In"):
                user = login_user(uid.strip(), pwd.strip())
                if user:
                    st.session_state['user'] = user
                    st.rerun()
                else:
                    st.error("Invalid credentials")
    else:
        user = st.session_state['user']
        
        with st.sidebar:
            st.write(f"Logged in: **{user['name']}**")
            if st.button("Logout"):
                st.session_state['user'] = None
                st.rerun()
                
        if user['role'] == "Admin":
            admin_dashboard()
        elif user['role'] == "Faculty":
            faculty_marking_dashboard(user)
        elif user['role'] == "Student":
            student_visual_dashboard(user)

if __name__ == "__main__":
    main()
