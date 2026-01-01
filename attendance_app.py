import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter
import datetime

# ==========================================
# 1. SETUP & AUTHENTICATION
# ==========================================

st.set_page_config(page_title="VTU Attendance (Reg 2022)", layout="wide")

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

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================

def get_roster(course_id):
    """Mock roster"""
    return [
        {"usn": "1MV20CS001", "name": "Rahul Sharma"},
        {"usn": "1MV20CS002", "name": "Priya Gowda"},
        {"usn": "1MV20CS003", "name": "Amit Verma"},
        {"usn": "1MV20CS004", "name": "Sneha Reddy"},
        {"usn": "1MV20CS005", "name": "Mohammed Ali"}
    ]

def upload_to_bucket(file_obj, path):
    blob = bucket.blob(path)
    blob.upload_from_string(file_obj.getvalue(), content_type=file_obj.type)
    blob.make_public()
    return blob.public_url

def batch_mark_attendance(course_id, date, total_students, absentees):
    batch = db.batch()
    
    # Log Session
    session_ref = db.collection('Class_Sessions').document()
    batch.set(session_ref, {
        "date": str(date),
        "course_id": course_id,
        "absentees": absentees,
        "timestamp": datetime.datetime.now()
    })
    
    # Update Student Summaries
    for student in total_students:
        usn = student['usn']
        is_absent = usn in absentees
        summary_ref = db.collection('Student_Summaries').document(usn)
        
        update_data = {
            f"{course_id}.total_classes": firestore.Increment(1),
            f"{course_id}.last_updated": datetime.datetime.now()
        }
        if not is_absent:
            update_data[f"{course_id}.attended_classes"] = firestore.Increment(1)
            
        batch.set(summary_ref, update_data, merge=True)

    batch.commit()

# ==========================================
# 3. FACULTY VIEW (Marking)
# ==========================================

def faculty_view():
    st.header("👨‍🏫 Faculty Dashboard (VTU Regs)")
    
    col1, col2 = st.columns(2)
    with col1:
        course = st.selectbox("Select Course", ["CS301 - Data Structures", "CS302 - Algorithms"])
        course_id = course.split(" - ")[0]
    with col2:
        date_sel = st.date_input("Date", datetime.date.today())

    st.divider()
    students = get_roster(course_id)
    
    with st.form("attendance_form"):
        attendance_status = {}
        st.write("Mark Absentees (Default is Present)")
        
        # Grid Layout for faster marking
        cols = st.columns(3)
        for i, student in enumerate(students):
            col = cols[i % 3]
            attendance_status[student['usn']] = col.checkbox(f"{student['usn']}", value=True)
            
        if st.form_submit_button("Submit Attendance"):
            absent_list = [usn for usn, present in attendance_status.items() if not present]
            with st.spinner("Syncing..."):
                batch_mark_attendance(course_id, date_sel, students, absent_list)
            st.success(f"Saved! {len(absent_list)} students marked absent.")

# ==========================================
# 4. STUDENT VIEW (Compliance Logic)
# ==========================================

def student_view():
    st.header("🎓 Student Compliance Portal")
    usn_input = st.text_input("Enter USN", "1MV20CS001").strip().upper()
    
    if usn_input:
        doc = db.collection('Student_Summaries').document(usn_input).get()
        
        if doc.exists:
            data = doc.to_dict()
            st.divider()
            
            for key, val in data.items():
                if isinstance(val, dict) and "total_classes" in val:
                    course_code = key
                    total = val['total_classes']
                    attended = val.get('attended_classes', 0)
                    pct = (attended / total) * 100 if total > 0 else 0
                    
                    # VTU LOGIC: Clause 22OB 3.7
                    # >= 85% : Safe
                    # 75% - 85% : Condonation Required (Medical/Sports)
                    # < 75% : DX Grade (Detained)
                    
                    if pct >= 85:
                        color = "green"
                        status = "✅ Safe"
                        buffer = int((attended - (0.85 * total)) / 0.85)
                        msg = f"Buffer: Can miss {buffer} classes."
                    elif 75 <= pct < 85:
                        color = "orange"
                        status = "⚠️ CONDONATION REQ"
                        msg = "Submit Medical Cert immediately to Principal."
                    else:
                        color = "red"
                        status = "🚫 DX GRADE RISK"
                        needed = int(((0.85 * total) - attended) / 0.15)
                        msg = f"CRITICAL: Attend next {needed} classes to reach 85%."
                    
                    st.markdown(f"### {course_code}: :{color}[{pct:.1f}%] - {status}")
                    st.progress(pct / 100)
                    st.caption(msg)
                    
                    # Condonation Upload Trigger
                    if color == "orange" or color == "red":
                        with st.expander(f"Upload Condonation Proof for {course_code}"):
                            uploaded_file = st.file_uploader(f"Medical Cert ({course_code})", type=['pdf', 'jpg'], key=course_code)
                            if uploaded_file and st.button("Submit to HOD", key=f"btn_{course_code}"):
                                url = upload_to_bucket(uploaded_file, f"condonation/{usn_input}/{course_code}_{uploaded_file.name}")
                                db.collection('Condonation_Requests').add({
                                    "usn": usn_input, "course": course_code,
                                    "url": url, "current_pct": pct,
                                    "status": "Pending Principal Approval"
                                })
                                st.success("Request Sent!")
                    st.divider()
        else:
            st.info("No records found.")

        # Activity Points Section (Clause 22OB 6.9.1)
        st.subheader("🏆 Activity Points (100 Pts Mandatory)")
        with st.expander("Upload AICTE Activity Cert"):
            act_file = st.file_uploader("Certificate", type=['pdf', 'jpg'])
            if act_file and st.button("Upload Activity"):
                url = upload_to_bucket(act_file, f"activity/{usn_input}/{act_file.name}")
                db.collection('Activity_Points').add({
                    "usn": usn_input, "url": url, "status": "Pending"
                })
                st.success("Activity recorded.")

# ==========================================
# 5. MAIN
# ==========================================

def main():
    st.sidebar.title("VTU Manager 2022")
    role = st.sidebar.radio("Role", ["Faculty", "Student"])
    if role == "Faculty":
        faculty_view()
    else:
        student_view()

if __name__ == "__main__":
    main()
