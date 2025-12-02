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
    """Saves Q1->CO1 mapping."""
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
    """
    THE CORE ENGINE: Calculates CO and PO attainment.
    """
    # 1. Fetch IA Marks
    marks_ref = db.collection('ia_marks').where('Code', '==', subject_code).stream()
    marks_data = [d.to_dict() for d in marks_ref]
    if not marks_data:
        return None, "No marks found."
    
    # 2. Fetch Patterns (to know Max Marks & CO mapping)
    patterns_ref = db.collection('assessment_patterns').where('subject_code', '==', subject_code).stream()
    patterns = {d.to_dict()['exam_type']: d.to_dict()['pattern'] for d in patterns_ref}
    
    if not patterns:
        return None, "No assessment pattern defined. Please configure IA Pattern."

    # 3. Aggregation Containers
    co_totals = {f"CO{i}": 0 for i in range(1, 7)}
    co_max_totals = {f"CO{i}": 0 for i in range(1, 7)}
    student_co_scores = {} # {USN: {CO1: obtained, CO1_max: max}}

    # 4. Process Every Student Record
    for record in marks_data:
        exam = record['Exam']
        usn = record['USN']
        scores = record['Scores'] # {Q1: 5, Q2: 8...}
        
        if exam not in patterns: continue
        pattern = patterns[exam] # {Q1: {co: CO1, max: 10}...}
        
        if usn not in student_co_scores:
            student_co_scores[usn] = {f"CO{i}": 0 for i in range(1, 7)}
            student_co_scores[usn].update({f"CO{i}_max": 0 for i in range(1, 7)})
            
        for q_key, obtained_mark in scores.items():
            if q_key in pattern:
                target_co = pattern[q_key]['co']
                max_mark = pattern[q_key]['max']
                
                # Add to Student Total
                student_co_scores[usn][target_co] += obtained_mark
                student_co_scores[usn][f"{target_co}_max"] += max_mark

    # 5. Calculate Attainment Level (Threshold: 60% of students scoring > 60%)
    # Simplified Logic: Average % of class for this demo
    co_attainment_results = {}
    
    for co in [f"CO{i}" for i in range(1, 7)]:
        total_students = len(student_co_scores)
        if total_students == 0: continue
        
        students_passed_threshold = 0
        
        for usn, data in student_co_scores.items():
            if data[f"{co}_max"] > 0:
                percentage = (data[co] / data[f"{co}_max"]) * 100
                if percentage >= 60: # Threshold
                    students_passed_threshold += 1
        
        attainment_percentage = (students_passed_threshold / total_students) * 100
        
        # Determine Level (1, 2, 3)
        level = 0
        if attainment_percentage >= 70: level = 3
        elif attainment_percentage >= 60: level = 2
        elif attainment_percentage >= 50: level = 1
        
        co_attainment_results[co] = level

    # 6. Map to POs
    copo_matrix = fetch_copo_mapping(subject_code)
    po_results = {}
    
    if copo_matrix:
        # copo_matrix is {PO1: [3, 2, 1...], PO2: ...} where index 0 is CO1
        for po_key, values in copo_matrix.items():
            if po_key == "CO_ID": continue # Skip ID column
            
            weighted_sum = 0
            count = 0
            
            for i, val in enumerate(values):
                co_key = f"CO{i+1}"
                if val and val > 0 and co_key in co_attainment_results:
                    weighted_sum += (val * co_attainment_results[co_key])
                    count += val
            
            if count > 0:
                po_results[po_key] = round(weighted_sum / count, 2)
            else:
                po_results[po_key] = 0

    return {"CO": co_attainment_results, "PO": po_results}, "Success"

# --- MODULES ---

def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")
    
    # 1. GLOBAL FILTERS
    with st.spinner("Loading Data..."):
        df_subjects = fetch_collection_as_df('setup_subjects')
        df_students = fetch_collection_as_df('setup_students')
    
    if df_subjects.empty:
        st.warning("No subjects found.")
        return

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
    
    # 2. TASK TABS
    tabs = st.tabs(["📝 Attendance", "⚙️ IA Pattern", "💯 IA Entry", "📊 CO-PO Mapping", "📈 Reports"])

    # === ATTENDANCE ===
    with tabs[0]:
        st.markdown(f"**Attendance: {current_sub_name}**")
        col1, col2 = st.columns(2)
        with col1: date_val = st.date_input("Date", datetime.now())
        with col2: time_slot = st.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "12:15-01:15", "02:00-03:00"])
        
        df_students['Section'] = df_students['Section'].astype(str).str.strip()
        section_students = df_students[df_students['Section'] == current_section].copy()
        
        if not section_students.empty:
            att_df = section_students[['USN', 'Name']].copy()
            att_df['Present'] = True
            edited_df = st.data_editor(att_df, column_config={"Present": st.column_config.CheckboxColumn(default=True)}, hide_index=True)
            if st.button("Submit Attendance"):
                records = [{"Date": str(date_val), "Time": time_slot, "Faculty": selected_faculty, "Section": current_section, "Code": current_sub_code, "USN": r['USN'], "Status": "Present" if r['Present'] else "Absent"} for _, r in edited_df.iterrows()]
                save_attendance_record(records)
                st.success("Saved!")

    # === IA PATTERN ===
    with tabs[1]:
        st.markdown("**Configure Assessment Pattern**")
        st.info("Define which Question maps to which CO.")
        
        exam_type_cfg = st.selectbox("Select Exam to Configure", ["IA Test 1", "IA Test 2", "IA Test 3", "Assignment 1"], key="cfg_exam")
        
        # Grid for Pattern
        # Rows: Q1, Q2, Q3, Q4. Cols: Max Marks, Mapped CO
        pattern_data = {
            "Question": ["Q1", "Q2", "Q3", "Q4"],
            "Max Marks": [10, 10, 10, 10],
            "Mapped CO": ["CO1", "CO2", "CO1", "CO2"]
        }
        df_pattern = pd.DataFrame(pattern_data)
        
        edited_pattern = st.data_editor(
            df_pattern,
            column_config={
                "Mapped CO": st.column_config.SelectboxColumn(options=[f"CO{i}" for i in range(1,7)])
            },
            hide_index=True,
            use_container_width=True
        )
        
        if st.button("💾 Save Pattern"):
            # Convert DF to Dict {Q1: {max: 10, co: CO1}}
            pat_dict = {}
            for _, row in edited_pattern.iterrows():
                pat_dict[row['Question']] = {"max": row['Max Marks'], "co": row['Mapped CO']}
            
            save_assessment_pattern(current_sub_code, exam_type_cfg, pat_dict)
            st.success(f"Pattern for {exam_type_cfg} Saved!")

    # === IA ENTRY ===
    with tabs[2]:
        st.markdown(f"**Enter Marks: {current_sub_code}**")
        exam_type_entry = st.selectbox("Select Assessment", ["IA Test 1", "IA Test 2", "IA Test 3", "Assignment 1"], key="entry_exam")
        
        # Check if pattern exists
        pat = fetch_assessment_pattern(current_sub_code, exam_type_entry)
        if not pat:
            st.error("⚠️ Pattern not defined for this exam. Go to 'IA Pattern' tab first!")
        else:
            df_students['Section'] = df_students['Section'].astype(str).str.strip()
            sec_stu = df_students[df_students['Section'] == current_section].copy()
            
            if not sec_stu.empty:
                # Prepare Columns based on Pattern Keys (Q1, Q2...)
                q_cols = sorted(list(pat.keys()))
                marks_df = sec_stu[['USN', 'Name']].copy()
                for q in q_cols:
                    marks_df[q] = 0
                
                edited_marks = st.data_editor(marks_df, disabled=["USN", "Name"], hide_index=True)
                
                if st.button("💾 Submit Marks"):
                    recs = []
                    for _, row in edited_marks.iterrows():
                        scores = {q: row[q] for q in q_cols}
                        total = sum(scores.values())
                        recs.append({
                            "USN": row['USN'], "Name": row['Name'], "Exam": exam_type_entry,
                            "Subject": current_sub_name, "Code": current_sub_code,
                            "Scores": scores, "Total_Obtained": total, "Timestamp": datetime.now().strftime("%Y-%m-%d")
                        })
                    save_ia_marks(recs, exam_type_entry, current_sub_code)
                    st.success("Marks Uploaded!")

    # === CO-PO MAPPING ===
    with tabs[3]:
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

    # === REPORTS ===
    with tabs[4]:
        st.header("📈 Course Attainment Report")
        if st.button("Generate Report"):
            with st.spinner("Calculating..."):
                results, msg = calculate_attainment(current_sub_code)
            
            if results:
                st.success("Calculation Complete!")
                
                col_a, col_b = st.columns(2)
                with col_a:
                    st.subheader("CO Attainment Levels")
                    st.dataframe(pd.DataFrame(list(results['CO'].items()), columns=['CO', 'Level (0-3)']), hide_index=True)
                
                with col_b:
                    st.subheader("Final PO Attainment")
                    st.dataframe(pd.DataFrame(list(results['PO'].items()), columns=['PO', 'Attained Value']), hide_index=True)
                    
                st.markdown("---")
                st.caption("*Logic: Level 3 if >70% students score >60%, Level 2 if >60%, Level 1 if >50%. PO = (CO-PO Mapping × CO Level) / 3*")
            else:
                st.error(msg)

def render_admin_space():
    st.subheader("⚙️ System Admin")
    with st.expander("Reset Data"):
        if st.button("Wipe All Data"):
            delete_collection(db.collection('setup_subjects'), 50)
            delete_collection(db.collection('setup_students'), 50)
            st.success("Wiped.")
            st.rerun()
            
    c1, c2 = st.columns(2)
    up_sub = c1.file_uploader("Subjects (Sheet 1)")
    up_stu = c2.file_uploader("Students (Sheet 3)")
    
    if st.button("Initialize"):
        if up_sub: upload_to_firestore('setup_subjects', pd.read_csv(up_sub))
        if up_stu:
            df = pd.read_csv(up_stu)
            if 'Section' not in df.columns: df.rename(columns={df.columns[3]: 'Section'}, inplace=True)
            upload_to_firestore('setup_students', df)
        st.success("Done")

def main():
    st.sidebar.title("RMS v4.0")
    menu = st.sidebar.radio("Menu", ["Faculty Dashboard", "System Admin"])
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "System Admin": render_admin_space()

if __name__ == "__main__":
    main()
