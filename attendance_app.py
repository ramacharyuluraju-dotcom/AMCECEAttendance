import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Dept. RMS (Stable)", layout="wide", page_icon="🎓")

# --- FIREBASE CONNECTION (SINGLETON PATTERN) ---
# We use st.cache_resource for the connection object itself to ensure it's created only once per session
@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        try:
            if "textkey" in st.secrets:
                secret_val = st.secrets["textkey"]
                if isinstance(secret_val, str):
                    try:
                        key_dict = json.loads(secret_val)
                    except json.JSONDecodeError:
                        st.error("Error decoding JSON key.")
                        return None
                else:
                    key_dict = secret_val
                
                cred = credentials.Certificate(key_dict)
                firebase_admin.initialize_app(cred)
            else:
                st.warning("⚠️ Firebase credentials not found.")
                return None
        except Exception as e:
            st.error(f"Failed to connect to Firebase: {e}")
            return None
    return firestore.client()

db = get_db()

if not db:
    st.stop()

# --- DATABASE OPERATIONS (OPTIMIZED) ---

def safe_firestore_write(operation_func, *args, **kwargs):
    """Wrapper to handle quota errors gracefully."""
    try:
        return operation_func(*args, **kwargs)
    except Exception as e:
        if "Quota exceeded" in str(e) or "Resource exhausted" in str(e):
            st.error("⚠️ System Quota Exceeded. Please try again later (Daily limit reached).")
        else:
            st.error(f"Database Error: {e}")
        return None

def delete_collection_batch(coll_ref, batch_size):
    docs = coll_ref.limit(batch_size).stream()
    deleted = 0
    for doc in docs:
        doc.reference.delete()
        deleted += 1
    if deleted >= batch_size:
        return delete_collection_batch(coll_ref, batch_size)

def save_question_paper(subject_code, exam_type, meta, questions, status="Draft"):
    uid = f"{subject_code}_{exam_type}"
    total_marks = sum([int(q['marks']) for q in questions if str(q['marks']).isdigit()])
    meta['maxMarks'] = total_marks
    
    data = {
        "subject_code": subject_code,
        "exam_type": exam_type,
        "meta": meta,
        "questions": questions,
        "status": status,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    db.collection('question_papers').document(uid).set(data)
    st.cache_data.clear() # Clear cache to reflect changes immediately
    return True

@st.cache_data(ttl=60) # Cache for 60 seconds to reduce reads
def fetch_question_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    doc = db.collection('question_papers').document(uid).get()
    if doc.exists:
        return doc.to_dict()
    return None

@st.cache_data(ttl=60)
def fetch_pending_papers():
    docs = db.collection('question_papers').where('status', '==', 'Submitted').stream()
    return [d.to_dict() for d in docs]

def approve_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    db.collection('question_papers').document(uid).update({"status": "Approved"})
    st.cache_data.clear()

def upload_to_firestore(collection_name, df):
    records = df.to_dict(orient='records')
    batch = db.batch()
    batch_count = 0
    coll_ref = db.collection(collection_name)
    
    for i, record in enumerate(records):
        doc_ref = coll_ref.document(str(i))
        batch.set(doc_ref, record)
        batch_count += 1
        if batch_count >= 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
    if batch_count > 0:
        batch.commit()
    st.cache_data.clear()
    return i + 1

@st.cache_data(ttl=300) # Cache heavy lists for 5 minutes
def fetch_collection_as_df(col):
    docs = db.collection(col).stream()
    data = [doc.to_dict() for doc in docs]
    return pd.DataFrame(data) if data else pd.DataFrame()

def save_attendance_record(records):
    batch = db.batch()
    for r in records:
        uid = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}".replace(" ","").replace("/","-")
        batch.set(db.collection('attendance_records').document(uid), r)
    batch.commit()
    st.cache_data.clear()

def save_ia_marks(records, exam_type, subject_code):
    batch = db.batch()
    for r in records:
        uid = f"{exam_type}_{subject_code}_{r['USN']}".replace(" ","")
        batch.set(db.collection('ia_marks').document(uid), r)
    batch.commit()

def save_copo_mapping(subject_code, mapping_data):
    db.collection('co_po_mappings').document(subject_code).set({"mapping": mapping_data})
    st.cache_data.clear()

@st.cache_data(ttl=300)
def fetch_copo_mapping(subject_code):
    doc = db.collection('co_po_mappings').document(subject_code).get()
    return doc.to_dict()['mapping'] if doc.exists else None

# --- NEW: COURSE METADATA (Stable Implementation) ---
def save_course_metadata(subject_code, co_statements):
    doc_ref = db.collection('course_metadata').document(subject_code)
    doc_ref.set({
        "subject_code": subject_code,
        "co_statements": co_statements,
        "last_updated": datetime.now().strftime("%Y-%m-%d")
    }, merge=True)
    st.cache_data.clear()

@st.cache_data(ttl=300)
def fetch_course_metadata(subject_code):
    doc = db.collection('course_metadata').document(subject_code).get()
    if doc.exists:
        return doc.to_dict()
    return {}

# --- HTML GENERATOR ---
def generate_qp_html(meta, questions):
    rows_html = ""
    for q in questions:
        rows_html += f"""<tr><td style="text-align: center;">{q['qNo']}</td><td>{q['text']}</td><td style="text-align: center;">{q['co']}</td><td style="text-align: center;">{q['bt']}</td><td style="text-align: center;">{q['marks']}</td></tr>"""
    
    return f"""
    <html><body style="font-family: serif; padding: 20px;">
    <div style="text-align: center; border-bottom: 2px solid black; margin-bottom: 20px;">
        <h2>AMC ENGINEERING COLLEGE</h2>
        <h4>Dept. of {meta.get('department', 'ECE')} | {meta.get('examName')}</h4>
    </div>
    <div style="display: flex; justify-content: space-between; margin-bottom: 20px;">
        <div><b>Course:</b> {meta.get('courseName')} ({meta.get('courseCode')})<br><b>Sem:</b> {meta.get('semester')}</div>
        <div style="text-align: right;"><b>Date:</b> {meta.get('date')}<br><b>Max Marks:</b> {meta.get('maxMarks')}</div>
    </div>
    <table style="width: 100%; border-collapse: collapse;" border="1">
        <tr style="background: #eee;"><th>Q.No</th><th>Question</th><th>CO</th><th>RBT</th><th>Marks</th></tr>
        {rows_html}
    </table>
    </body></html>
    """

# --- STUDENT MANAGEMENT ---
def register_students_bulk(df, ay, batch, semester, section):
    records = df.to_dict(orient='records')
    batch_write = db.batch()
    coll_ref = db.collection('setup_students')
    count = 0
    for rec in records:
        usn = str(rec['USN']).strip().upper()
        name = str(rec['Name']).strip()
        student_data = {"USN": usn, "Name": name, "Status": "Active", "Section": section, "AY": ay}
        batch_write.set(coll_ref.document(usn), student_data, merge=True)
        count += 1
        if count % 400 == 0:
            batch_write.commit()
            batch_write = db.batch()
    if count > 0: batch_write.commit()
    st.cache_data.clear()
    return count

def update_student_status(usn, status):
    db.collection('setup_students').document(usn).update({"Status": status})
    st.cache_data.clear()

# --- UI MODULES ---

def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")
    
    with st.spinner("Syncing..."):
        df_subjects = fetch_collection_as_df('setup_subjects')
        df_students_raw = fetch_collection_as_df('setup_students')
    
    if df_subjects.empty: st.warning("No subjects."); return

    if not df_students_raw.empty and 'Status' in df_students_raw.columns:
        df_students = df_students_raw[df_students_raw['Status'] == 'Active']
    else: df_students = df_students_raw

    # Filters
    faculty_list = sorted(df_subjects['Faculty Name'].unique().tolist()) if 'Faculty Name' in df_subjects.columns else []
    selected_faculty = st.selectbox("Faculty", faculty_list)
    faculty_data = df_subjects[df_subjects['Faculty Name'] == selected_faculty]
    
    if faculty_data.empty: return
    faculty_data['Display_Label'] = faculty_data['Section'].astype(str) + " - " + faculty_data['Subject Name']
    selected_label = st.selectbox("Class", faculty_data['Display_Label'].unique())
    
    class_info = faculty_data[faculty_data['Display_Label'] == selected_label].iloc[0]
    current_sec = str(class_info['Section']).strip()
    current_sub = class_info['Subject Name']
    current_code = class_info['Subject Code']
    
    st.divider()
    tabs = st.tabs(["📝 Attendance", "📄 Question Paper", "💯 IA Entry", "📊 Reports", "📋 CO-PO", "📘 Course File"])

    # 1. ATTENDANCE
    with tabs[0]:
        c1, c2 = st.columns(2)
        d_val = c1.date_input("Date")
        t_slot = c2.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "02:00-03:00"])
        
        if not df_students.empty:
            df_students['Section'] = df_students['Section'].astype(str).str.strip()
            sec_stu = df_students[df_students['Section'] == current_sec].copy()
            if not sec_stu.empty:
                att = sec_stu[['USN', 'Name']].copy(); att['Present'] = True
                edt = st.data_editor(att, hide_index=True, key="att_editor")
                if st.button("Submit Attendance"):
                    recs = [{"Date":str(d_val), "Time":t_slot, "Faculty":selected_faculty, "Section":current_sec, "Code":current_code, "USN":r['USN'], "Status":"Present" if r['Present'] else "Absent"} for _,r in edt.iterrows()]
                    save_attendance_record(recs); st.success("Saved!")
            else: st.info("No students.")

    # 2. QP SETTER
    with tabs[1]:
        exam_type = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="qp_exam")
        existing_qp = fetch_question_paper(current_code, exam_type)
        
        # Initialize or Load
        if existing_qp:
            status = existing_qp.get('status', 'Draft')
            st.info(f"Status: **{status}**")
            current_qs = pd.DataFrame(existing_qp['questions'])
            meta_default = existing_qp['meta']
        else:
            st.info("Status: **New Draft**")
            current_qs = pd.DataFrame({"qNo": ["1a", "1b"], "text": ["", ""], "marks": [0, 0], "co": ["CO1", "CO1"], "bt": ["L1", "L1"]})
            meta_default = {"date": str(datetime.now().date()), "duration": "90 Mins"}

        c1, c2 = st.columns(2)
        m_date = c1.text_input("Date", meta_default.get('date'), key="m_date")
        m_dur = c2.text_input("Duration", meta_default.get('duration'), key="m_dur")
        
        edited_qs = st.data_editor(current_qs, num_rows="dynamic", use_container_width=True, key=f"qp_edit_{current_code}")
        
        # Actions
        qs_list = edited_qs.to_dict(orient='records')
        meta = {"examName": exam_type, "courseName": current_sub, "courseCode": current_code, "semester": current_sec, "date": m_date, "duration": m_dur}
        
        if st.button("💾 Save Draft"):
            safe_firestore_write(save_question_paper, current_code, exam_type, meta, qs_list, "Draft")
            st.success("Draft Saved.")
        
        if st.button("🚀 Submit to HOD"):
            safe_firestore_write(save_question_paper, current_code, exam_type, meta, qs_list, "Submitted")
            st.success("Submitted!")
            st.rerun()

    # 3. IA ENTRY
    with tabs[2]:
        exam_entry = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="ia_entry")
        qp = fetch_question_paper(current_code, exam_entry)
        
        if not qp or qp.get('status') != "Approved":
            st.warning("QP must be APPROVED by HOD to enter marks.")
        else:
            if not df_students.empty:
                df_students['Section'] = df_students['Section'].astype(str).str.strip()
                sec_stu = df_students[df_students['Section'] == current_sec].copy()
                if not sec_stu.empty:
                    q_cols = [q['qNo'] for q in qp['questions']]
                    marks_df = sec_stu[['USN', 'Name']].copy()
                    for c in q_cols: marks_df[c] = 0
                    
                    edited_marks = st.data_editor(marks_df, disabled=["USN", "Name"], hide_index=True, key="ia_editor")
                    if st.button("Submit Marks"):
                        recs = []
                        for _, row in edited_marks.iterrows():
                            scores = {col: row[col] for col in q_cols}
                            recs.append({"USN": row['USN'], "Name": row['Name'], "Exam": exam_entry, "Subject": current_sub, "Code": current_code, "Scores": scores, "Total": sum(scores.values())})
                        safe_firestore_write(save_ia_marks, recs, exam_entry, current_code)
                        st.success("Saved!")

    # 4. REPORTS
    with tabs[3]: st.info("Reports module.")

    # 5. CO-PO
    with tabs[4]:
        cols = [f"PO{i}" for i in range(1, 13)] + ["PSO1", "PSO2"]
        rows = [f"CO{i}" for i in range(1, 7)]
        existing = fetch_copo_mapping(current_code)
        
        if existing: df_copo = pd.DataFrame(existing)
        else: df_copo = pd.DataFrame(0, index=rows, columns=cols); df_copo.insert(0, "CO_ID", rows)
        
        edited_copo = st.data_editor(df_copo, hide_index=True, key="copo_edit")
        if st.button("Save Mapping"):
            safe_firestore_write(save_copo_mapping, current_code, edited_copo.to_dict(orient='list'))
            st.success("Saved!")

    # 6. COURSE FILE (SAFE RE-INTEGRATION)
    with tabs[5]:
        st.markdown("### 📘 Course File & Planning")
        cf_tabs = st.tabs(["CO Statements", "Download"])
        
        with cf_tabs[0]:
            st.markdown("**Define CO Statements**")
            meta = fetch_course_metadata(current_code)
            co_texts = meta.get('co_statements', {})
            
            # Using unique keys based on Subject Code to prevent duplicate ID crashes
            updated_cos = {}
            for i in range(1, 7):
                k = f"CO{i}"
                val = co_texts.get(k, "")
                updated_cos[k] = st.text_area(f"{k}", value=val, height=68, key=f"txt_{current_code}_{i}")
            
            if st.button("Save Statements"):
                safe_firestore_write(save_course_metadata, current_code, updated_cos)
                st.success("Saved.")

        with cf_tabs[1]:
            st.markdown("### Download Course File")
            if st.button("Generate Report"):
                report_txt = f"COURSE FILE: {current_sub}\nCODE: {current_code}\n"
                report_txt += "-"*20 + "\n\nCO STATEMENTS:\n"
                for k, v in updated_cos.items():
                    report_txt += f"{k}: {v}\n"
                st.download_button("Download Text File", report_txt, "course_file.txt")

def render_hod():
    st.subheader("🔍 HOD")
    pending = fetch_pending_papers()
    if not pending: st.info("No pending papers."); return
    
    for p in pending:
        with st.expander(f"{p['meta']['courseCode']} - {p['exam_type']}"):
            if st.button(f"Approve {p['meta']['courseCode']}", key=f"app_{p['meta']['courseCode']}"):
                approve_paper(p['subject_code'], p['exam_type'])
                st.success("Approved!")
                st.rerun()

def render_admin():
    st.subheader("⚙️ Admin")
    t1, t2, t3 = st.tabs(["Register", "Master Uploads", "Manage"])
    
    with t1:
        st.markdown("### Register Students")
        c1, c2, c3 = st.columns(3)
        ay = c1.selectbox("AY", ["2024-25", "2025-26"])
        sem = c2.selectbox("Sem", [1,2,3,4,5,6,7,8])
        sec = c3.text_input("Sec", "A")
        up = st.file_uploader("CSV", type=['csv'])
        if st.button("Register") and up:
            df = pd.read_csv(up)
            df.columns = df.columns.str.strip()
            if 'USN' in df.columns:
                c = register_students_bulk(df, ay, "2022", sem, sec)
                st.success(f"Registered {c}.")
    
    with t2:
        st.markdown("### Upload Subjects")
        up_sub = st.file_uploader("Subjects CSV")
        if st.button("Upload") and up_sub:
            upload_to_firestore('setup_subjects', pd.read_csv(up_sub))
            st.success("Done.")

def main():
    st.sidebar.title("RMS v7.3 (Stable)")
    menu = st.sidebar.radio("Role", ["Faculty Dashboard", "HOD / Scrutiny", "System Admin"])
    
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "HOD / Scrutiny": render_hod()
    elif menu == "System Admin": render_admin()

if __name__ == "__main__":
    main()
