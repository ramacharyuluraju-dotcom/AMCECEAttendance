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
    """Legacy upload for Subjects/Syllabus."""
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
    st.cache_data.clear()

# --- NEW: IA, PATTERN & REPORT FUNCTIONS ---

def save_copo_mapping(subject_code, mapping_data):
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

def save_assessment_pattern(subject_code, exam_type, pattern_data):
    uid = f"{subject_code}_{exam_type}"
    db.collection('assessment_patterns').document(uid).set({
        "subject_code": subject_code,
        "exam_type": exam_type,
        "pattern": pattern_data
    })

def fetch_assessment_pattern(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    doc = db.collection('assessment_patterns').document(uid).get()
    if doc.exists:
        return doc.to_dict()['pattern']
    return None

def save_ia_marks(records, exam_type, subject_code):
    batch = db.batch()
    coll = db.collection('ia_marks')
    for rec in records:
        uid = f"{exam_type}_{subject_code}_{rec['USN']}"
        uid = uid.replace(" ", "")
        batch.set(coll.document(uid), rec)
    batch.commit()

def calculate_attainment(subject_code):
    # 1. Fetch Marks
    marks_ref = db.collection('ia_marks').where('Code', '==', subject_code).stream()
    marks_data = [d.to_dict() for d in marks_ref]
    if not marks_data: return None, "No marks found."
    
    # 2. Fetch Patterns
    patterns_ref = db.collection('assessment_patterns').where('subject_code', '==', subject_code).stream()
    patterns = {d.to_dict()['exam_type']: d.to_dict()['pattern'] for d in patterns_ref}
    if not patterns: return None, "IA Pattern not configured."

    # 3. Aggregate
    student_co_scores = {} 
    
    for record in marks_data:
        exam = record['Exam']
        usn = record['USN']
        scores = record['Scores']
        
        if exam not in patterns: continue
        pattern = patterns[exam]
        
        if usn not in student_co_scores:
            student_co_scores[usn] = {f"CO{i}": 0 for i in range(1, 7)}
            student_co_scores[usn].update({f"CO{i}_max": 0 for i in range(1, 7)})
            
        for q_key, obtained_mark in scores.items():
            if q_key in pattern:
                target_co = pattern[q_key]['co']
                max_mark = pattern[q_key]['max']
                student_co_scores[usn][target_co] += obtained_mark
                student_co_scores[usn][f"{target_co}_max"] += max_mark

    # 4. CO Attainment Level
    co_attainment_results = {}
    for co in [f"CO{i}" for i in range(1, 7)]:
        total_students = len(student_co_scores)
        if total_students == 0: continue
        students_passed = 0
        for usn, data in student_co_scores.items():
            if data[f"{co}_max"] > 0:
                if (data[co] / data[f"{co}_max"]) * 100 >= 60:
                    students_passed += 1
        
        perc = (students_passed / total_students) * 100
        if perc >= 70: level = 3
        elif perc >= 60: level = 2
        elif perc >= 50: level = 1
        else: level = 0
        co_attainment_results[co] = level

    # 5. PO Attainment
    copo_matrix = fetch_copo_mapping(subject_code)
    po_results = {}
    if copo_matrix:
        for po_key, values in copo_matrix.items():
            if po_key == "CO_ID": continue
            weighted_sum = 0
            count = 0
            for i, val in enumerate(values):
                co_key = f"CO{i+1}"
                if val and val > 0 and co_key in co_attainment_results:
                    weighted_sum += (val * co_attainment_results[co_key])
                    count += val
            po_results[po_key] = round(weighted_sum / count, 2) if count > 0 else 0

    return {"CO": co_attainment_results, "PO": po_results}, "Success"

# --- STUDENT MANAGEMENT FUNCTIONS (v5.0 CORE) ---

def register_students_bulk(df, ay, batch, semester, section):
    """Registers students with metadata using USN as ID."""
    records = df.to_dict(orient='records')
    batch_write = db.batch()
    coll_ref = db.collection('setup_students')
    count = 0
    
    for rec in records:
        # Standardize Data
        usn = str(rec['USN']).strip().upper()
        name = str(rec['Name']).strip()
        
        student_data = {
            "USN": usn,
            "Name": name,
            "Academic_Year": ay,
            "Batch": batch,
            "Semester": semester,
            "Section": section,
            "Status": "Active", # Default status
            "Last_Updated": datetime.now().strftime("%Y-%m-%d")
        }
        
        # Use USN as Document ID (Prevents Duplicates automatically)
        doc_ref = coll_ref.document(usn)
        batch_write.set(doc_ref, student_data, merge=True)
        count += 1
        
        if count % 400 == 0:
            batch_write.commit()
            batch_write = db.batch()
            
    if count > 0:
        batch_write.commit()
    return count

def update_student_status(usn, status):
    """Updates status to Active/Detained/Alumni"""
    doc_ref = db.collection('setup_students').document(usn)
    if doc_ref.get().exists:
        doc_ref.update({"Status": status})
        return True
    return False

# --- MODULES ---

def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")
    
    with st.spinner("Loading Data..."):
        df_subjects = fetch_collection_as_df('setup_subjects')
        df_students_raw = fetch_collection_as_df('setup_students')
    
    if df_subjects.empty:
        st.warning("No subjects configured.")
        return

    # --- INTELLIGENT FILTERING ---
    # Only show ACTIVE students to Faculty
    if not df_students_raw.empty and 'Status' in df_students_raw.columns:
        df_students = df_students_raw[df_students_raw['Status'] == 'Active']
    else:
        df_students = df_students_raw

    # Faculty & Subject Selection
    faculty_list = sorted(df_subjects['Faculty Name'].unique().tolist())
    selected_faculty = st.selectbox("Select Faculty Name", faculty_list)
    
    faculty_data = df_subjects[df_subjects['Faculty Name'] == selected_faculty]
    if faculty_data.empty: return

    faculty_data['Display_Label'] = faculty_data['Section'].astype(str) + " - " + faculty_data['Subject Name']
    selected_label = st.selectbox("Select Active Class", faculty_data['Display_Label'].unique())
    
    class_info = faculty_data[faculty_data['Display_Label'] == selected_label].iloc[0]
    current_section = str(class_info['Section']).strip()
    current_sub_name = class_info['Subject Name']
    current_sub_code = class_info['Subject Code']
    
    st.markdown("---")
    
    # Task Tabs
    tabs = st.tabs(["📝 Attendance", "📊 History", "⚙️ IA Pattern", "💯 IA Entry", "📊 CO-PO Mapping", "📈 Reports"])

    # 1. ATTENDANCE
    with tabs[0]:
        st.markdown(f"**Mark Attendance: {current_sub_name} ({current_section})**")
        c1, c2 = st.columns(2)
        with c1: date_val = st.date_input("Date", datetime.now())
        with c2: time_slot = st.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "12:15-01:15", "02:00-03:00"])
        
        # Filter Logic
        if not df_students.empty and 'Section' in df_students.columns:
            df_students['Section'] = df_students['Section'].astype(str).str.strip()
            # Match section strictly
            section_students = df_students[df_students['Section'] == current_section].copy()
            
            if not section_students.empty:
                att_df = section_students[['USN', 'Name']].copy()
                att_df['Present'] = True
                edited_df = st.data_editor(att_df, column_config={"Present": st.column_config.CheckboxColumn(default=True)}, hide_index=True)
                
                if st.button("Submit Attendance", type="primary"):
                    records = []
                    for _, row in edited_df.iterrows():
                        records.append({
                            "Date": str(date_val), "Time": time_slot, "Faculty": selected_faculty, 
                            "Section": current_section, "Code": current_sub_code, 
                            "USN": row['USN'], "Status": "Present" if row['Present'] else "Absent"
                        })
                    save_attendance_record(records)
                    st.success("Attendance Saved!")
            else:
                st.info(f"No active students found in Section {current_section}.")
        else:
            st.warning("Student list empty. Please register students in System Admin.")

    # 2. HISTORY
    with tabs[1]:
        st.markdown(f"**History: {current_sub_code}**")
        if st.button("🔄 Load History"):
            all_recs = fetch_collection_as_df('attendance_records')
            if not all_recs.empty:
                if 'Code' in all_recs.columns:
                    sub_recs = all_recs[all_recs['Code'] == current_sub_code]
                else:
                    sub_recs = all_recs
                
                if not sub_recs.empty:
                    st.dataframe(sub_recs.sort_values(by=['Date', 'Time'], ascending=False), hide_index=True)
                    csv = sub_recs.to_csv(index=False).encode('utf-8')
                    st.download_button("📥 Download CSV", csv, "attendance.csv", "text/csv")
                else:
                    st.info("No history for this subject.")

    # 3. IA PATTERN
    with tabs[2]:
        st.markdown("**Configure Assessment Pattern**")
        exam_type_cfg = st.selectbox("Select Exam", ["IA Test 1", "IA Test 2", "IA Test 3", "Assignment 1"], key="cfg")
        
        # Default sub-questions
        if 'pattern_data' not in st.session_state:
             st.session_state.pattern_data = {
                "Question": ["1a", "1b", "1c", "2a", "2b", "2c", "3a", "3b", "3c", "4a", "4b", "4c"],
                "Max Marks": [5, 5, 0, 5, 5, 0, 5, 5, 0, 5, 5, 0],
                "Mapped CO": ["CO1", "CO1", "CO1", "CO2", "CO2", "CO2", "CO3", "CO3", "CO3", "CO4", "CO4", "CO4"]
            }
        
        saved_pat = fetch_assessment_pattern(current_sub_code, exam_type_cfg)
        if saved_pat:
            qs = sorted(list(saved_pat.keys()))
            df_pattern = pd.DataFrame({
                "Question": qs,
                "Max Marks": [saved_pat[q]['max'] for q in qs],
                "Mapped CO": [saved_pat[q]['co'] for q in qs]
            })
        else:
             df_pattern = pd.DataFrame(st.session_state.pattern_data)
        
        edited_pattern = st.data_editor(
            df_pattern,
            column_config={
                "Mapped CO": st.column_config.SelectboxColumn(options=[f"CO{i}" for i in range(1,7)]),
                "Max Marks": st.column_config.NumberColumn(min_value=0, max_value=20)
            },
            hide_index=True,
            num_rows="dynamic",
            use_container_width=True
        )
        
        if st.button("💾 Save Pattern"):
            pat_dict = {}
            for _, row in edited_pattern.iterrows():
                if row['Question'] and str(row['Question']).strip():
                    pat_dict[str(row['Question']).strip()] = {"max": row['Max Marks'], "co": row['Mapped CO']}
            save_assessment_pattern(current_sub_code, exam_type_cfg, pat_dict)
            st.success("Pattern Saved!")

    # 4. IA ENTRY
    with tabs[3]:
        st.markdown(f"**Enter Marks: {current_sub_code}**")
        exam_type_entry = st.selectbox("Select Assessment", ["IA Test 1", "IA Test 2", "IA Test 3", "Assignment 1"], key="entry")
        
        pat = fetch_assessment_pattern(current_sub_code, exam_type_entry)
        if not pat:
            st.error("⚠️ Configure IA Pattern first.")
        else:
            if not df_students.empty:
                df_students['Section'] = df_students['Section'].astype(str).str.strip()
                sec_stu = df_students[df_students['Section'] == current_section].copy()
                
                if not sec_stu.empty:
                    q_cols = sorted(list(pat.keys()))
                    marks_df = sec_stu[['USN', 'Name']].copy()
                    for q in q_cols: marks_df[q] = 0
                    
                    edited_marks = st.data_editor(marks_df, disabled=["USN", "Name"], hide_index=True)
                    
                    if st.button("💾 Submit Marks"):
                        recs = []
                        for _, row in edited_marks.iterrows():
                            scores = {q: row[q] for q in q_cols}
                            recs.append({
                                "USN": row['USN'], "Name": row['Name'], "Exam": exam_type_entry,
                                "Subject": current_sub_name, "Code": current_sub_code,
                                "Scores": scores, "Total_Obtained": sum(scores.values()), "Timestamp": datetime.now().strftime("%Y-%m-%d")
                            })
                        save_ia_marks(recs, exam_type_entry, current_sub_code)
                        st.success("Marks Uploaded!")
            else:
                st.info("No active students.")

    # 5. CO-PO
    with tabs[4]:
        st.markdown("**Course Articulation Matrix**")
        cols = [f"PO{i}" for i in range(1, 13)] + ["PSO1", "PSO2"]
        rows = [f"CO{i}" for i in range(1, 7)]
        
        existing = fetch_copo_mapping(current_sub_code)
        if existing:
            df_copo = pd.DataFrame(existing)
        else:
            df_copo = pd.DataFrame(0, index=rows, columns=cols)
            df_copo.insert(0, "CO_ID", rows)
            
        edited_copo = st.data_editor(df_copo, hide_index=True)
        if st.button("💾 Save Mapping"):
            save_copo_mapping(current_sub_code, edited_copo.to_dict(orient='list'))
            st.success("Saved!")

    # 6. REPORTS
    with tabs[5]:
        st.header("📈 Attainment Report")
        if st.button("Generate Report"):
            with st.spinner("Calculating..."):
                results, msg = calculate_attainment(current_sub_code)
            if results:
                c1, c2 = st.columns(2)
                c1.dataframe(pd.DataFrame(list(results['CO'].items()), columns=['CO', 'Level']), hide_index=True)
                c2.dataframe(pd.DataFrame(list(results['PO'].items()), columns=['PO', 'Value']), hide_index=True)
            else:
                st.error(msg)

def render_admin_space():
    st.subheader("⚙️ System Admin & Student Lifecycle")
    
    tabs = st.tabs(["🎓 Student Registration", "🚫 Detain/Manage", "🏫 Master Uploads", "📥 Global Reports"])
    
    # TAB 1: REGISTRATION
    with tabs[0]:
        st.markdown("### 🎓 Register Students for Academic Year")
        st.info("Use this to onboard new batches or add lateral entry students.")
        
        c1, c2, c3 = st.columns(3)
        with c1: ay = st.selectbox("Academic Year", ["2023-24", "2024-25", "2025-26"], index=1)
        with c2: batch = st.selectbox("Batch (Joining Year)", ["2021", "2022", "2023", "2024"])
        with c3: sem = st.selectbox("Current Semester", [1, 2, 3, 4, 5, 6, 7, 8], index=2)
        
        target_section = st.text_input("Section to Assign (e.g., 3A, 5B)", placeholder="3A").strip()
        
        st.markdown("#### Option A: Bulk Upload (CSV)")
        st.caption("Required Columns: `USN`, `Name`")
        up_file = st.file_uploader("Upload Student List CSV", type=['csv'])
        
        if st.button("🚀 Register Batch"):
            if up_file and target_section:
                try:
                    df = pd.read_csv(up_file)
                    df.columns = df.columns.str.strip()
                    # Basic validation
                    if 'USN' in df.columns and 'Name' in df.columns:
                        count = register_students_bulk(df, ay, batch, sem, target_section)
                        st.success(f"Successfully registered {count} students to {target_section} (AY {ay}).")
                    else:
                        st.error("CSV must contain 'USN' and 'Name' columns.")
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.error("Please provide Section and File.")

        st.markdown("#### Option B: Single Student Entry")
        with st.form("single_reg"):
            s_usn = st.text_input("USN").strip().upper()
            s_name = st.text_input("Name").strip()
            if st.form_submit_button("Register Single Student"):
                if s_usn and s_name and target_section:
                    df_single = pd.DataFrame([{"USN": s_usn, "Name": s_name}])
                    register_students_bulk(df_single, ay, batch, sem, target_section)
                    st.success(f"Registered {s_name}!")
                else:
                    st.error("Missing fields.")

    # TAB 2: MANAGE
    with tabs[1]:
        st.markdown("### 🚫 Manage Student Status")
        search_q = st.text_input("Search USN").strip().upper()
        if search_q:
            doc = db.collection('setup_students').document(search_q).get()
            if doc.exists:
                d = doc.to_dict()
                st.write(f"**{d.get('Name')}** ({d.get('Section')}) | Status: **{d.get('Status')}**")
                new_stat = st.selectbox("Update Status", ["Active", "Detained", "Alumni", "Dropped"], index=0)
                if st.button("Update Status"):
                    update_student_status(search_q, new_stat)
                    st.success("Updated!")
            else:
                st.warning("Student not found.")

    # TAB 3: MASTER UPLOADS
    with tabs[2]:
        st.markdown("### 🏫 Master Data (Subjects)")
        up_sub = st.file_uploader("Upload Subjects (Sheet 1)", type=['csv'])
        if st.button("Upload Subjects"):
            if up_sub:
                c = upload_to_firestore('setup_subjects', pd.read_csv(up_sub))
                st.success(f"Uploaded {c} subjects.")

        st.divider()
        with st.expander("⚠️ Danger Zone"):
            st.warning("Only use this to wipe the entire database for a fresh start.")
            if st.button("🗑️ Wipe ALL Data"):
                delete_collection(db.collection('setup_subjects'), 50)
                delete_collection(db.collection('setup_students'), 50)
                st.success("Database Wiped.")

    # TAB 4: REPORTS
    with tabs[3]:
        st.markdown("### 📥 Global Reports")
        if st.button("Download Full Attendance"):
            df = fetch_collection_as_df('attendance_records')
            if not df.empty:
                st.download_button("Download CSV", df.to_csv(index=False).encode('utf-8'), "full_data.csv", "text/csv")
            else:
                st.info("No data.")

def main():
    st.sidebar.title("RMS v5.0")
    menu = st.sidebar.radio("Navigate", ["Faculty Dashboard", "System Admin"])
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "System Admin": render_admin_space()

if __name__ == "__main__":
    main()
