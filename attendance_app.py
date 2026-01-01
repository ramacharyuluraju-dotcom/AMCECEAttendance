import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter
import datetime
import json

# ==========================================
# 1. SETUP & AUTHENTICATION
# ==========================================

st.set_page_config(page_title="VTU Attendance & Compliance", layout="wide")

# Check if Firebase is already initialized to avoid "App already exists" error
if not firebase_admin._apps:
    # --------------------------------------------------------------------------
    # AUTHENTICATION LOGIC (Cloud vs. Local)
    # --------------------------------------------------------------------------
    try:
        # CASE 1: STREAMLIT CLOUD
        # We try to access the secrets. If this fails, we go to the 'except' block.
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
    except Exception:
        # CASE 2: LOCAL MACHINE
        # If secrets aren't found, we look for the JSON file on your computer.
        # Make sure 'firebase_key.json' is in the same folder as app.py
        cred = credentials.Certificate("firebase_key.json")
        
    # REPLACE THIS with your actual bucket name from Firebase Console -> Storage
    # It usually looks like: 'your-project-id.appspot.com'
    BUCKET_NAME = "your-project-id.appspot.com" 
    
    firebase_admin.initialize_app(cred, {
        'storageBucket': BUCKET_NAME
    })

db = firestore.client()
bucket = storage.bucket()

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================

def get_roster(course_id):
    """Mock roster for demonstration"""
    return [
        {"usn": "1MV20CS001", "name": "Rahul Sharma"},
        {"usn": "1MV20CS002", "name": "Priya Gowda"},
        {"usn": "1MV20CS003", "name": "Amit Verma"},
        {"usn": "1MV20CS004", "name": "Sneha Reddy"},
        {"usn": "1MV20CS005", "name": "Mohammed Ali"}
    ]

def upload_to_bucket(file_obj, path):
    """Uploads file to Firebase Storage and returns public URL"""
    blob = bucket.blob(path)
    blob.upload_from_string(
        file_obj.getvalue(), 
        content_type=file_obj.type
    )
    # Note: makes file publicly accessible via URL
    blob.make_public()
    return blob.public_url

def batch_mark_attendance(course_id, date, total_students, absentees):
    """Writes session log + Updates student summaries (Hybrid Approach)"""
    batch = db.batch()
    
    # A. Create Session Log
    session_ref = db.collection('Class_Sessions').document()
    batch.set(session_ref, {
        "date": str(date),
        "course_id": course_id,
        "absentees": absentees,
        "timestamp": datetime.datetime.now()
    })
    
    # B. Fan-out Update (Update individual student summaries)
    for student in total_students:
        usn = student['usn']
        is_absent = usn in absentees
        
        summary_ref = db.collection('Student_Summaries').document(usn)
        
        # Firestore Increment to avoid race conditions
        update_data = {
            f"{course_id}.total_classes": firestore.Increment(1),
            f"{course_id}.last_updated": datetime.datetime.now()
        }
        
        if not is_absent:
            update_data[f"{course_id}.attended_classes"] = firestore.Increment(1)
            
        batch.set(summary_ref, update_data, merge=True)

    batch.commit()

# ==========================================
# 3. PAGE VIEWS
# ==========================================

def faculty_view():
    st.header("👨‍🏫 Faculty Dashboard")
    
    col1, col2 = st.columns(2)
    with col1:
        course = st.selectbox("Select Course", ["CS301 - Data Structures", "CS302 - Algorithms"])
        course_id = course.split(" - ")[0]
    with col2:
        date_sel = st.date_input("Date", datetime.date.today())

    st.divider()
    students = get_roster(course_id)
    st.subheader(f"Marking Attendance for: {course}")
    
    with st.form("attendance_form"):
        attendance_status = {}
        for student in students:
            c1, c2 = st.columns([3, 1])
            c1.write(f"**{student['usn']}** - {student['name']}")
            # Default is Checked (Present)
            attendance_status[student['usn']] = c2.checkbox("Present", value=True, key=student['usn'])
            
        submitted = st.form_submit_button("Submit Attendance")
        
        if submitted:
            absent_list = [usn for usn, present in attendance_status.items() if not present]
            with st.spinner("Syncing with Firestore..."):
                batch_mark_attendance(course_id, date_sel, students, absent_list)
            st.success(f"✅ Attendance Saved! {len(absent_list)} students marked absent.")

def student_view():
    st.header("🎓 Student Portal")
    usn_input = st.text_input("Enter USN to view Dashboard", "1MV20CS001").strip().upper()
    
    if usn_input:
        # Read from 'Student_Summaries' collection (Fast Read)
        doc = db.collection('Student_Summaries').document(usn_input).get()
        
        if doc.exists:
            data = doc.to_dict()
            st.divider()
            st.subheader(f"Attendance Status: {usn_input}")
            
            for key, val in data.items():
                if isinstance(val, dict) and "total_classes" in val:
                    course_code = key
                    total = val['total_classes']
                    attended = val.get('attended_classes', 0)
                    pct = (attended / total) * 100 if total > 0 else 0
                    
                    st.write(f"**{course_code}**: {pct:.1f}% ({attended}/{total})")
                    st.progress(pct / 100)
        else:
            st.info("No attendance records found yet.")

        st.subheader("📤 Upload Certificates")
        with st.expander("Submit Activity Point / Medical Cert"):
            act_type = st.selectbox("Document Type", ["Activity Point", "Medical Certificate"])
            uploaded_file = st.file_uploader("Choose PDF/Image", type=['pdf', 'png', 'jpg'])
            
            if uploaded_file and st.button("Upload Document"):
                with st.spinner("Uploading..."):
                    file_path = f"students/{usn_input}/{act_type}_{uploaded_file.name}"
                    url = upload_to_bucket(uploaded_file, file_path)
                    
                    db.collection('Student_Documents').add({
                        "usn": usn_input,
                        "type": act_type,
                        "url": url,
                        "timestamp": datetime.datetime.now()
                    })
                    st.success("Uploaded successfully!")

# ==========================================
# 4. MAIN APP LOGIC
# ==========================================

def main():
    st.sidebar.title("VTU Manager")
    role = st.sidebar.radio("Select Role", ["Faculty", "Student"])
    
    if role == "Faculty":
        faculty_view()
    else:
        student_view()

if __name__ == "__main__":
    main()
