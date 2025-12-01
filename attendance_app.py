import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Student Attendance", layout="centered", page_icon="📝")

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
    """Uploads a dataframe to a Firestore collection."""
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
    """
    Saves attendance records with DUPLICATE PREVENTION.
    It creates a unique ID for each student's entry based on:
    Date_Section_Subject_Time_USN
    """
    batch = db.batch()
    collection_ref = db.collection('attendance_records')
    
    for record in records:
        # 1. Create a Unique ID string
        # Example ID: "2023-11-01_3A_BEC302_09:00-10:00_1AM24EC001"
        unique_id = f"{record['Date']}_{record['Section']}_{record['Code']}_{record['Time']}_{record['USN']}"
        
        # 2. Clean the ID (remove spaces or problematic chars)
        unique_id = unique_id.replace(" ", "").replace("/", "-")
        
        # 3. Use this ID to set the document
        # If it exists, it Overwrites. If not, it Creates.
        doc_ref = collection_ref.document(unique_id)
        batch.set(doc_ref, record)
        
    batch.commit()

# --- MAIN APP ---

def main():
    st.title("☁️ Class Attendance App")

    # 1. Load Data
    with st.spinner("Syncing..."):
        df_subjects = fetch_collection_as_df('setup_subjects')
        df_students = fetch_collection_as_df('setup_students')

    system_ready = not df_subjects.empty and not df_students.empty

    # 2. Tabs
    if not system_ready:
        st.warning("⚠️ System not initialized. Go to 'System Setup' tab.")
        tab1, tab2, tab3 = st.tabs(["⚙️ System Setup", "📝 Mark Attendance", "📊 History"])
    else:
        tab1, tab2, tab3 = st.tabs(["📝 Mark Attendance", "📊 History", "⚙️ System Setup"])

    # --- MARK ATTENDANCE ---
    with tab1 if system_ready else tab2:
        if system_ready:
            st.markdown("### 1. Select Class")
            if 'Faculty Name' in df_subjects.columns:
                faculty_list = sorted(df_subjects['Faculty Name'].unique().tolist())
                selected_faculty = st.selectbox("Select Faculty", faculty_list)
                
                faculty_data = df_subjects[df_subjects['Faculty Name'] == selected_faculty]
                
                if not faculty_data.empty:
                    faculty_data['Display_Label'] = (
                        faculty_data['Section'].astype(str) + " - " + 
                        faculty_data['Subject Name']
                    )
                    selected_label = st.selectbox("Select Subject", faculty_data['Display_Label'].unique())
                    
                    class_info = faculty_data[faculty_data['Display_Label'] == selected_label].iloc[0]
                    current_section = str(class_info['Section']).strip()
                    current_sub_name = class_info['Subject Name']
                    current_sub_code = class_info['Subject Code']
                    
                    col1, col2 = st.columns(2)
                    with col1: date_val = st.date_input("Date", datetime.now())
                    with col2: time_slot = st.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "12:15-01:15", "02:00-03:00", "03:00-04:00"])
                    
                    st.divider()
                    st.markdown(f"### 2. Students: {current_section}")
                    
                    # Filter Students
                    df_students['Section'] = df_students['Section'].astype(str).str.strip()
                    section_students = df_students[df_students['Section'] == current_section].copy()
                    
                    if not section_students.empty:
                        # Editor
                        att_df = section_students[['USN', 'Name']].copy()
                        att_df['Present'] = True
                        
                        edited_df = st.data_editor(
                            att_df,
                            column_config={
                                "Present": st.column_config.CheckboxColumn("Present?", default=True),
                                "USN": st.column_config.TextColumn("USN", disabled=True),
                                "Name": st.column_config.TextColumn("Name", disabled=True)
                            },
                            hide_index=True,
                            use_container_width=True
                        )
                        
                        if st.button("✅ Submit Attendance", type="primary", use_container_width=True):
                            with st.spinner("Saving..."):
                                records = []
                                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                for _, row in edited_df.iterrows():
                                    records.append({
                                        "Date": str(date_val),
                                        "Time": time_slot,
                                        "Faculty": selected_faculty,
                                        "Section": current_section,
                                        "Subject": current_sub_name,
                                        "Code": current_sub_code,
                                        "USN": row['USN'],
                                        "Name": row['Name'],
                                        "Status": "Present" if row['Present'] else "Absent",
                                        "Timestamp": timestamp
                                    })
                                save_attendance_record(records)
                            st.success("Attendance Saved!")
                    else:
                        st.error(f"No students found for Section {current_section}")
        else:
            st.info("Please initialize system first.")

    # --- HISTORY ---
    with tab1 if not system_ready else tab2:
        st.markdown("### History")
        if st.button("🔄 Refresh"):
            hist = fetch_collection_as_df('attendance_records')
            if not hist.empty:
                st.dataframe(hist, use_container_width=True)
                csv = hist.to_csv(index=False).encode('utf-8')
                st.download_button("📥 Download CSV", csv, "attendance.csv", "text/csv")
            else:
                st.info("No records found.")

    # --- SETUP ---
    with tab1 if not system_ready else tab3:
        st.header("⚙️ System Initialization")
        
        # RESET SECTION
        with st.expander("⚠️ Danger Zone: Reset System"):
            st.warning("Only use this when starting a NEW SEMESTER. It will delete all student/subject data.")
            if st.button("🗑️ Wipe All Master Data"):
                with st.spinner("Deleting old data..."):
                    delete_collection(db.collection('setup_subjects'), 50)
                    delete_collection(db.collection('setup_students'), 50)
                st.success("System Wiped. Ready for new files.")
                st.rerun()

        st.info("Upload Sheet 1 (Subjects) and Sheet 3 (Students) here.")
        
        up_sub = st.file_uploader("Upload Sheet 1", type=['csv'])
        up_stu = st.file_uploader("Upload Sheet 3", type=['csv'])
        
        if st.button("🚀 Initialize / Update", type="primary"):
            if up_sub and up_stu:
                try:
                    df_sub = pd.read_csv(up_sub)
                    df_sub.columns = df_sub.columns.str.strip()
                    
                    df_stu = pd.read_csv(up_stu)
                    df_stu.columns = df_stu.columns.str.strip()
                    
                    if 'Section' not in df_stu.columns:
                        cols = list(df_stu.columns)
                        if 'Name' in cols:
                            idx = cols.index('Name')
                            if len(cols) > idx + 1:
                                df_stu.rename(columns={cols[idx+1]: 'Section'}, inplace=True)
                        elif len(cols) >= 4:
                             df_stu.rename(columns={cols[3]: 'Section'}, inplace=True)

                    with st.spinner("Uploading to Cloud..."):
                        c1 = upload_to_firestore('setup_subjects', df_sub)
                        c2 = upload_to_firestore('setup_students', df_stu)
                    st.success(f"Done! Uploaded {c1} subjects and {c2} students.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

if __name__ == "__main__":
    main()
