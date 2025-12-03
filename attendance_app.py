import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Dept. Record Management System", layout="wide", page_icon="🎓")

# --- FIREBASE CONNECTION (Singleton Pattern) ---
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
if not db: st.stop()

# --- UTILS & HTML GENERATOR ---

def generate_qp_html(meta, questions):
    """Generates the Standardized HTML Question Paper."""
    rows_html = ""
    for q in questions:
        rows_html += f"""
        <tr>
            <td style="text-align: center;">{q['qNo']}</td>
            <td>{q['text']}</td>
            <td style="text-align: center;">{q['co']}</td>
            <td style="text-align: center;">{q['bt']}</td>
            <td style="text-align: center;">{q['marks']}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Times New Roman', serif; margin: 0; padding: 20px; }}
            .header {{ text-align: center; margin-bottom: 20px; border-bottom: 2px solid black; padding-bottom: 10px; }}
            .header h1 {{ margin: 0; font-size: 24px; text-transform: uppercase; }}
            .header h3 {{ margin: 5px 0; font-size: 16px; font-weight: normal; }}
            .meta-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; font-size: 14px; }}
            .meta-right {{ text-align: right; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ border: 1px solid black; padding: 8px; font-size: 14px; vertical-align: top; }}
            th {{ background-color: #f2f2f2; }}
            .signatures {{ margin-top: 50px; display: flex; justify-content: space-between; text-align: center; }}
            .sig-line {{ border-top: 1px solid black; width: 150px; margin: 0 auto; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>AMC ENGINEERING COLLEGE</h1>
            <h3>(AUTONOMOUS)</h3>
            <h3>Department of {meta.get('department', 'ECE')}</h3>
            <h2 style="margin: 10px 0;">{meta.get('examName', 'Internal Assessment')}</h2>
        </div>
        
        <div class="meta-grid">
            <div>
                <strong>Course:</strong> {meta.get('courseName', '')}<br>
                <strong>Code:</strong> {meta.get('courseCode', '')}<br>
                <strong>Sem/Sec:</strong> {meta.get('semester', '')}
            </div>
            <div class="meta-right">
                <strong>Date:</strong> {meta.get('date', '')}<br>
                <strong>Max Marks:</strong> {meta.get('maxMarks', '50')}<br>
                <strong>Duration:</strong> {meta.get('duration', '90 mins')}
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 50px;">Q.No</th>
                    <th>Question</th>
                    <th style="width: 50px;">CO</th>
                    <th style="width: 50px;">RBT</th>
                    <th style="width: 50px;">Marks</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>

        <div class="signatures">
            <div>
                <div style="height: 40px;"></div>
                <div class="sig-line"></div>
                <div>Course Teacher</div>
            </div>
            <div>
                <div style="height: 40px;"></div>
                <div class="sig-line"></div>
                <div>Scrutinized By</div>
            </div>
            <div>
                <div style="height: 40px;"></div>
                <div class="sig-line"></div>
                <div>HOD / PAC</div>
            </div>
        </div>
    </body>
    </html>
    """

# --- DATABASE OPERATIONS (Optimized) ---

def safe_firestore_write(operation_func, *args, **kwargs):
    try:
        return operation_func(*args, **kwargs)
    except Exception as e:
        if "Quota exceeded" in str(e):
            st.error("⚠️ System Quota Exceeded. Try again later.")
        else:
            st.error(f"Error: {e}")
        return None

def delete_collection(coll_ref, batch_size):
    docs = coll_ref.limit(batch_size).stream()
    deleted = 0
    for doc in docs:
        doc.reference.delete()
        deleted += 1
    if deleted >= batch_size: return delete_collection(coll_ref, batch_size)

def save_question_paper(subject_code, exam_type, meta, questions, status="Draft"):
    uid = f"{subject_code}_{exam_type}"
    total_marks = sum([int(q['marks']) for q in questions if str(q['marks']).isdigit()])
    meta['maxMarks'] = total_marks
    data = {"subject_code": subject_code, "exam_type": exam_type, "meta": meta, "questions": questions, "status": status, "timestamp": datetime.now().strftime("%Y-%m-%d")}
    db.collection('question_papers').document(uid).set(data)
    st.cache_data.clear()
    return True

@st.cache_data(ttl=60)
def fetch_question_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    doc = db.collection('question_papers').document(uid).get()
    return doc.to_dict() if doc.exists else None

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
    if batch_count > 0: batch.commit()
    st.cache_data.clear()
    return i + 1

@st.cache_data(ttl=300)
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
    st.cache_data.clear()

def save_copo_mapping(subject_code, mapping_data):
    db.collection('co_po_mappings').document(subject_code).set({"mapping": mapping_data})
    st.cache_data.clear()

@st.cache_data(ttl=300)
def fetch_copo_mapping(subject_code):
    doc = db.collection('co_po_mappings').document(subject_code).get()
    return doc.to_dict()['mapping'] if doc.exists else None

def calculate_attainment(subject_code):
    marks_ref = db.collection('ia_marks').where('Code', '==', subject_code).stream()
    marks_data = [d.to_dict() for d in marks_ref]
    if not marks_data: return None, "No marks found."
    
    exam_types = set([m['Exam'] for m in marks_data])
    patterns = {}
    for et in exam_types:
        qp = fetch_question_paper(subject_code, et)
        if qp and qp.get('status') == 'Approved':
            pat_dict = {}
            for q in qp['questions']: pat_dict[str(q['qNo'])] = {'co': q['co'], 'max': int(q['marks'])}
            patterns[et] = pat_dict
    
    if not patterns: return None, "No Approved QP found."

    student_co_scores = {} 
    for record in marks_data:
        exam = record['Exam']; usn = record['USN']; scores = record['Scores']
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

    co_attainment_results = {}
    for co in [f"CO{i}" for i in range(1, 7)]:
        total_students = len(student_co_scores)
        if total_students == 0: continue
        students_passed = 0
        for usn, data in student_co_scores.items():
            if data[f"{co}_max"] > 0:
                if (data[co] / data[f"{co}_max"]) * 100 >= 60: students_passed += 1
        
        perc = (students_passed / total_students) * 100
        if perc >= 70: level = 3
        elif perc >= 60: level = 2
        elif perc >= 50: level = 1
        else: level = 0
        co_attainment_results[co] = level

    copo_matrix = fetch_copo_mapping(subject_code)
    po_results = {}
    if copo_matrix:
        for po_key, values in copo_matrix.items():
            if po_key == "CO_ID": continue
            weighted_sum = 0; count = 0
            for i, val in enumerate(values):
                co_key = f"CO{i+1}"
                val_int = int(val) if str(val).isdigit() else 0 
                if val_int > 0 and co_key in co_attainment_results:
                    weighted_sum += (val_int * co_attainment_results[co_key])
                    count += val_int
            po_results[po_key] = round(weighted_sum / count, 2) if count > 0 else 0

    return {"CO": co_attainment_results, "PO": po_results}, "Success"

# --- STUDENT MANAGEMENT ---
def register_students_bulk(df, ay, batch, semester, section):
    records = df.to_dict(orient='records')
    batch_write = db.batch()
    coll_ref = db.collection('setup_students')
    count = 0
    for rec in records:
        usn = str(rec['USN']).strip().upper()
        name = str(rec['Name']).strip()
        student_data = {"USN": usn, "Name": name, "AY": ay, "Batch": batch, "Sem": semester, "Section": section, "Status": "Active"}
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
    return True

# --- UI MODULES ---

def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")
    with st.spinner("Loading..."):
        df_subjects = fetch_collection_as_df('setup_subjects')
        df_students_raw = fetch_collection_as_df('setup_students')
    if df_subjects.empty: st.warning("No subjects."); return

    if not df_students_raw.empty and 'Status' in df_students_raw.columns:
        df_students = df_students_raw[df_students_raw['Status'] == 'Active']
    else: df_students = df_students_raw

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
    tabs = st.tabs(["📝 Attendance", "📄 Question Paper", "💯 IA Entry", "📊 Reports", "📋 CO-PO"])

    with tabs[0]:
        st.markdown(f"**Attendance: {current_sec}**")
        c1, c2 = st.columns(2)
        d_val = c1.date_input("Date")
        t_slot = c2.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "02:00-03:00"])
        if not df_students.empty:
            df_students['Section'] = df_students['Section'].astype(str).str.strip()
            sec_stu = df_students[df_students['Section'] == current_sec].copy()
            if not sec_stu.empty:
                att = sec_stu[['USN', 'Name']].copy(); att['Present'] = True
                edt = st.data_editor(att, hide_index=True, key="att_edit")
                if st.button("Submit Attd"):
                    recs = [{"Date":str(d_val), "Time":t_slot, "Faculty":selected_faculty, "Section":current_sec, "Code":current_code, "USN":r['USN'], "Status":"Present" if r['Present'] else "Absent"} for _,r in edt.iterrows()]
                    safe_firestore_write(save_attendance_record, recs)
                    st.success("Saved!")
            else: st.info("No students.")

    with tabs[1]:
        st.markdown("### 📄 Question Paper Setter")
        exam_type = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="qp_exam")
        existing_qp = fetch_question_paper(current_code, exam_type)
        if existing_qp:
            st.info(f"Status: **{existing_qp.get('status')}**")
            current_qs = pd.DataFrame(existing_qp['questions'])
            meta_default = existing_qp['meta']
        else:
            st.info("Status: **New Draft**")
            current_qs = pd.DataFrame({"qNo": ["1a", "1b"], "text": ["", ""], "marks": [0, 0], "co": ["CO1", "CO1"], "bt": ["L1", "L1"]})
            meta_default = {"date": str(datetime.now().date()), "duration": "90 Mins"}

        c1, c2 = st.columns(2)
        m_date = c1.text_input("Date", meta_default.get('date'), key="m_date")
        m_dur = c2.text_input("Duration", meta_default.get('duration'), key="m_dur")
        
        edited_qs = st.data_editor(current_qs, num_rows="dynamic", use_container_width=True, key="qp_edit")
        qs_list = edited_qs.to_dict(orient='records')
        meta = {"examName": exam_type, "courseName": current_sub, "courseCode": current_code, "semester": current_sec, "date": m_date, "duration": m_dur, "department": "ECE"}
        
        if st.button("👁️ Preview"):
            st.components.v1.html(generate_qp_html(meta, qs_list), height=500, scrolling=True)
        
        c1, c2 = st.columns(2)
        if c1.button("💾 Save Draft"): safe_firestore_write(save_question_paper, current_code, exam_type, meta, qs_list, "Draft"); st.success("Saved.")
        if c2.button("🚀 Submit to HOD"): safe_firestore_write(save_question_paper, current_code, exam_type, meta, qs_list, "Submitted"); st.success("Submitted!")

    with tabs[2]:
        st.markdown("### 💯 Marks Entry")
        exam_entry = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="ia_entry")
        qp = fetch_question_paper(current_code, exam_entry)
        if not qp or qp.get('status') != "Approved": st.warning("QP must be APPROVED.")
        else:
            if not df_students.empty:
                df_students['Section'] = df_students['Section'].astype(str).str.strip()
                sec_stu = df_students[df_students['Section'] == current_sec].copy()
                if not sec_stu.empty:
                    q_cols = [q['qNo'] for q in qp['questions']]
                    marks_df = sec_stu[['USN', 'Name']].copy()
                    for c in q_cols: marks_df[c] = 0
                    edited_marks = st.data_editor(marks_df, disabled=["USN", "Name"], hide_index=True, key="ia_edit")
                    if st.button("Submit Marks"):
                        recs = []
                        for _, row in edited_marks.iterrows():
                            scores = {col: row[col] for col in q_cols}
                            recs.append({"USN": row['USN'], "Name": row['Name'], "Exam": exam_entry, "Subject": current_sub, "Code": current_code, "Scores": scores, "Total": sum(scores.values())})
                        safe_firestore_write(save_ia_marks, recs, exam_entry, current_code); st.success("Saved!")

    with tabs[3]:
        st.header("📈 Attainment Report")
        if st.button("Generate"):
            with st.spinner("Calculating..."):
                results, msg = calculate_attainment(current_code)
            if results:
                c1, c2 = st.columns(2)
                c1.dataframe(pd.DataFrame(list(results['CO'].items()), columns=['CO', 'Level']), hide_index=True)
                c2.dataframe(pd.DataFrame(list(results['PO'].items()), columns=['PO', 'Val']), hide_index=True)
            else: st.error(msg)

    # --- CO-PO MAPPING (STRICT v6.1 - NO UPLOAD) ---
    with tabs[4]: 
        st.markdown("### 📋 Course Articulation Matrix (CO-PO)")
        cols = [f"PO{i}" for i in range(1, 13)] + ["PSO1", "PSO2"]
        rows = [f"CO{i}" for i in range(1, 7)]
        existing = fetch_copo_mapping(current_code)
        if existing: df_copo = pd.DataFrame(existing)
        else: df_copo = pd.DataFrame(0, index=rows, columns=cols); df_copo.insert(0, "CO_ID", rows)
        edited_copo = st.data_editor(df_copo, hide_index=True, use_container_width=True, key="copo_edit")
        if st.button("💾 Save CO-PO Mapping"):
            safe_firestore_write(save_copo_mapping, current_code, edited_copo.to_dict(orient='list'))
            st.success("Mapping Saved!")

def render_hod_scrutiny():
    st.subheader("🔍 HOD")
    pending = fetch_pending_papers()
    if not pending: st.info("No pending papers."); return
    for p in pending:
        with st.expander(f"{p['meta']['courseCode']} - {p['exam_type']}"):
            if st.button(f"Approve {p['meta']['courseCode']}", key=f"app_{p['meta']['courseCode']}"):
                approve_paper(p['subject_code'], p['exam_type']); st.success("Approved!"); st.rerun()

def render_admin_space():
    st.subheader("⚙️ Admin")
    t1, t2, t3 = st.tabs(["Register", "Master Uploads", "Manage"])
    with t1:
        c1, c2, c3 = st.columns(3)
        ay = c1.selectbox("AY", ["24-25", "25-26"]); sem = c2.selectbox("Sem", [1,2,3,4,5,6,7,8]); sec = c3.text_input("Sec", "A")
        up = st.file_uploader("CSV", type=['csv'])
        if st.button("Register") and up:
            c = register_students_bulk(pd.read_csv(up), ay, "2022", sem, sec); st.success(f"Registered {c}.")
    with t2:
        up_sub = st.file_uploader("Subjects CSV")
        if st.button("Upload") and up_sub: upload_to_firestore('setup_subjects', pd.read_csv(up_sub)); st.success("Done.")
    with t3:
        if st.button("Download DB"): 
            st.download_button("CSV", fetch_collection_as_df('attendance_records').to_csv().encode('utf-8'), "data.csv")

def main():
    st.sidebar.title("RMS v6.1")
    menu = st.sidebar.radio("Role", ["Faculty Dashboard", "HOD / Scrutiny", "System Admin"])
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "HOD / Scrutiny": render_hod_scrutiny()
    elif menu == "System Admin": render_admin_space()

if __name__ == "__main__":
    main()
