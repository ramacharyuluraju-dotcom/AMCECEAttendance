import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Dept. Record Management System", layout="wide", page_icon="🎓")

# --- FIREBASE CONNECTION ---
if not firebase_admin._apps:
    try:
        if "textkey" in st.secrets:
            secret_val = st.secrets["textkey"]
            if isinstance(secret_val, str):
                try:
                    key_dict = json.loads(secret_val)
                except json.JSONDecodeError:
                     st.error("Error decoding JSON key.")
                     st.stop()
            else:
                key_dict = secret_val
            
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred)
        else:
            st.warning("⚠️ Firebase credentials not found.")
            st.stop()
    except Exception as e:
        st.error(f"Failed to connect to Firebase: {e}")
        st.stop()

db = firestore.client()

# --- DATABASE FUNCTIONS ---

def delete_collection(coll_ref, batch_size):
    """Deletes all documents in a collection."""
    docs = coll_ref.limit(batch_size).stream()
    deleted = 0
    for doc in docs:
        doc.reference.delete()
        deleted += 1
    if deleted >= batch_size:
        return delete_collection(coll_ref, batch_size)

def upload_to_firestore(collection_name, df):
    records = df.to_dict(orient='records')
    batch = db.batch()
    batch_count = 0
    total_uploaded = 0
    coll_ref = db.collection(collection_name)
    
    for i, record in enumerate(records):
        doc_ref = coll_ref.document(str(i))
        batch.set(doc_ref, record)
        batch_count += 1
        total_uploaded += 1
        if batch_count >= 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
    if batch_count > 0:
        batch.commit()
    return total_uploaded

@st.cache_data(ttl=600)
def fetch_collection_as_df(collection_name):
    docs = db.collection(collection_name).stream()
    data = [doc.to_dict() for doc in docs]
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)

def save_attendance_record(records):
    batch = db.batch()
    collection_ref = db.collection('attendance_records')
    for record in records:
        unique_id = f"{record['Date']}_{record['Section']}_{record['Code']}_{record['Time']}_{record['USN']}"
        unique_id = unique_id.replace(" ", "").replace("/", "-")
        doc_ref = collection_ref.document(unique_id)
        batch.set(doc_ref, record)
    batch.commit()

# --- NEW: IA & CO-PO FUNCTIONS ---

def save_copo_mapping(subject_code, mapping_data):
    """Saves the CO-PO matrix for a subject."""
    doc_ref = db.collection('co_po_mappings').document(subject_code)
    doc_ref.set({
        "subject_code": subject_code,
        "mapping": mapping_data,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

def fetch_copo_mapping(subject_code):
    doc = db.collection('co_po_mappings').document(subject_code).get()
    if doc.exists:
        return doc.to_dict()['mapping']
    return None

def save_ia_marks(records, exam_type, subject_code):
    """Saves marks. ID structure: Exam_Subject_USN"""
    batch = db.batch()
    coll = db.collection('ia_marks')
    for rec in records:
        # ID: IA1_BEC302_1AM24EC001
        uid = f"{exam_type}_{subject_code}_{rec['USN']}"
        uid = uid.replace(" ", "")
        batch.set(coll.document(uid), rec)
    batch.commit()

# --- MODULES ---

def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")
    
    # 1. GLOBAL FILTERS (Select once, use everywhere)
    with st.spinner("Loading Data..."):
        df_subjects = fetch_collection_as_df('setup_subjects')
        df_students = fetch_collection_as_df('setup_students')
    
    if df_subjects.empty:
        st.warning("No subjects found. Please go to System Admin.")
        return

    # A. Select Faculty
    faculty_list = sorted(df_subjects['Faculty Name'].unique().tolist())
    selected_faculty = st.selectbox("Select Faculty Name", faculty_list)
    
    # B. Select Subject
    faculty_data = df_subjects[df_subjects['Faculty Name'] == selected_faculty]
    if faculty_data.empty:
        st.info("No subjects assigned.")
        return

    faculty_data['Display_Label'] = faculty_data['Section'].astype(str) + " - " + faculty_data['Subject Name']
    selected_label = st.selectbox("Select Active Class", faculty_data['Display_Label'].unique())
    
    # Extract Context
    class_info = faculty_data[faculty_data['Display_Label'] == selected_label].iloc[0]
    current_section = str(class_info['Section']).strip()
    current_sub_name = class_info['Subject Name']
    current_sub_code = class_info['Subject Code']
    
    st.markdown("---")
    
    # 2. TASK TABS
    task_tab = st.radio("Select Task", ["📝 Attendance", "📊 CO-PO Mapping", "💯 Internal Assessment (IA)"], horizontal=True)

    # === TASK 1: ATTENDANCE ===
    if task_tab == "📝 Attendance":
        st.markdown(f"**Marking Attendance for: {current_sub_name} ({current_section})**")
        col1, col2 = st.columns(2)
        with col1: date_val = st.date_input("Date", datetime.now())
        with col2: time_slot = st.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "12:15-01:15", "02:00-03:00", "03:00-04:00"])
        
        df_students['Section'] = df_students['Section'].astype(str).str.strip()
        section_students = df_students[df_students['Section'] == current_section].copy()
        
        if not section_students.empty:
            att_df = section_students[['USN', 'Name']].copy()
            att_df['Present'] = True
            
            edited_df = st.data_editor(att_df, column_config={"Present": st.column_config.CheckboxColumn(default=True)}, hide_index=True, use_container_width=True)
            
            if st.button("Submit Attendance", type="primary"):
                records = []
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for _, row in edited_df.iterrows():
                    records.append({
                        "Date": str(date_val), "Time": time_slot, "Faculty": selected_faculty,
                        "Section": current_section, "Subject": current_sub_name, "Code": current_sub_code,
                        "USN": row['USN'], "Name": row['Name'], "Status": "Present" if row['Present'] else "Absent", "Timestamp": ts
                    })
                save_attendance_record(records)
                st.success("Attendance Saved!")
        else:
            st.error(f"No students in Section {current_section}")

    # === TASK 2: CO-PO MAPPING ===
    elif task_tab == "📊 CO-PO Mapping":
        st.markdown(f"**Course Articulation Matrix for {current_sub_code}**")
        st.info("Map your Course Outcomes (COs) to Program Outcomes (POs). Level: 1 (Low), 2 (Medium), 3 (High).")
        
        # Default Matrix Structure
        cols = [f"PO{i}" for i in range(1, 13)] + ["PSO1", "PSO2"]
        rows = [f"CO{i}" for i in range(1, 7)]
        
        # Check if exists
        existing_data = fetch_copo_mapping(current_sub_code)
        
        if existing_data:
            df_copo = pd.DataFrame(existing_data)
        else:
            # Create Empty DF
            df_copo = pd.DataFrame(0, index=rows, columns=cols)
            df_copo.insert(0, "CO_ID", rows) # Add ID column for display
            
        # Editor
        edited_copo = st.data_editor(df_copo, hide_index=True, use_container_width=True)
        
        if st.button("💾 Save Mapping"):
            # Convert to dict for JSON storage
            data_dict = edited_copo.to_dict(orient='list')
            save_copo_mapping(current_sub_code, data_dict)
            st.success("Mapping Saved Successfully!")

    # === TASK 3: INTERNAL ASSESSMENT (IA) ===
    elif task_tab == "💯 Internal Assessment (IA)":
        st.markdown(f"**IA Entry for {current_sub_code}**")
        
        # Configuration
        col_exam, col_mode = st.columns(2)
        with col_exam:
            exam_type = st.selectbox("Select Assessment", ["IA Test 1", "IA Test 2", "IA Test 3", "Assignment 1", "Assignment 2"])
        
        # Setup the Grid based on your Excel format
        # Faculty enters marks Question-wise (Q1, Q2, Q3) to map to COs later
        st.caption("Enter marks for each question. Leave 0 if not attempted.")
        
        df_students['Section'] = df_students['Section'].astype(str).str.strip()
        section_students = df_students[df_students['Section'] == current_section].copy()
        
        if section_students.empty:
            st.error("No students found.")
        else:
            # Create entry sheet
            # We assume a standard structure based on your file (Q1a, Q1b... is too wide for mobile, so we group by main Qs first)
            # You can expand this columns list based on the exact structure you want active
            entry_cols = ['Q1', 'Q2', 'Q3', 'Q4', 'Total'] 
            
            # Prepare DF
            marks_df = section_students[['USN', 'Name']].copy()
            for c in entry_cols:
                marks_df[c] = 0  # Initialize with 0
            
            # Show Editor
            edited_marks = st.data_editor(
                marks_df, 
                disabled=["USN", "Name", "Total"], # Total should be auto-calc but streamlit editor doesn't auto-sum yet. We sum in python.
                hide_index=True, 
                use_container_width=True
            )
            
            if st.button(f"💾 Submit {exam_type} Marks"):
                final_records = []
                for _, row in edited_marks.iterrows():
                    # Calculate total in backend to be safe
                    total = row['Q1'] + row['Q2'] + row['Q3'] + row['Q4']
                    
                    final_records.append({
                        "USN": row['USN'],
                        "Name": row['Name'],
                        "Exam": exam_type,
                        "Subject": current_sub_name,
                        "Code": current_sub_code,
                        "Scores": {
                            "Q1": row['Q1'], "Q2": row['Q2'], "Q3": row['Q3'], "Q4": row['Q4']
                        },
                        "Total_Obtained": total,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d")
                    })
                
                save_ia_marks(final_records, exam_type, current_sub_code)
                st.success(f"{exam_type} Marks uploaded successfully!")

def render_admin_space():
    st.subheader("⚙️ System Admin & Setup")
    
    with st.expander("⚠️ Danger Zone: Reset Semester Data"):
        st.warning("Use this to Wipe Data for a New Semester.")
        if st.button("🗑️ Wipe All Master Data"):
            with st.spinner("Deleting..."):
                delete_collection(db.collection('setup_subjects'), 50)
                delete_collection(db.collection('setup_students'), 50)
                delete_collection(db.collection('setup_syllabus'), 50)
            st.success("All System Data Wiped.")
            st.rerun()

    st.markdown("### Upload Master Files")
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown("**1. Faculty/Subjects**")
        up_sub = st.file_uploader("Upload Sheet 1", type=['csv'], key="sub")
    
    with col2:
        st.markdown("**2. Student List**")
        up_stu = st.file_uploader("Upload Sheet 3", type=['csv'], key="stu")
        
    if st.button("🚀 Initialize System", type="primary"):
        try:
            msg = ""
            if up_sub:
                df = pd.read_csv(up_sub)
                df.columns = df.columns.str.strip()
                c = upload_to_firestore('setup_subjects', df)
                msg += f"Subjects: {c}. "
            if up_stu:
                df = pd.read_csv(up_stu)
                df.columns = df.columns.str.strip()
                if 'Section' not in df.columns:
                    cols = list(df.columns)
                    if len(cols) >= 4: df.rename(columns={cols[3]: 'Section'}, inplace=True)
                c = upload_to_firestore('setup_students', df)
                msg += f"Students: {c}."
            
            if msg:
                st.success(f"Success! {msg}")
            else:
                st.warning("Upload a file first.")
        except Exception as e:
            st.error(f"Error: {e}")

# --- MAIN APP ROUTER ---

def main():
    st.sidebar.title("📚 Dept. RMS")
    menu = st.sidebar.radio("Navigate", ["Faculty Dashboard", "Student Space", "System Admin"])
    st.sidebar.divider()
    
    if menu == "Faculty Dashboard":
        render_faculty_dashboard()
    elif menu == "System Admin":
        render_admin_space()
    elif menu == "Student Space":
        st.info("Student dashboard under construction.")

if __name__ == "__main__":
    main()
