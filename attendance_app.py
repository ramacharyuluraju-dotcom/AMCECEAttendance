import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# ============================= CONFIG =============================
st.set_page_config(page_title="Dept. Record Management System", layout="wide", page_icon="🎓")
st.title("🏫 RMS v6.3 – Firestore Optimized & Production Ready")

# ============================= FIREBASE INIT =============================
if not firebase_admin._apps:
    try:
        if "textkey" in st.secrets:
            secret_val = st.secrets["textkey"]
            key_dict = json.loads(secret_val) if isinstance(secret_val, str) else secret_val
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred)
        else:
            st.error("Firebase key missing in secrets")
            st.stop()
    except Exception as e:
        st.error(f"Firebase init failed: {e}")
        st.stop()

db = firestore.client()

# ============================= OPTIMIZED FETCH FUNCTIONS =============================
@st.cache_data(ttl=3600, show_spinner=False)
def get_subjects_by_faculty(faculty_name: str) -> pd.DataFrame:
    docs = db.collection('setup_subjects').where('Faculty Name', '==', faculty_name.strip()).stream()
    data = [d.to_dict() for d in docs]
    df = pd.DataFrame(data) if data else pd.DataFrame()
    if not df.empty:
        df['Display_Label'] = df['Section'].astype(str).str.upper() + " - " + df['Subject Name']
    return df

@st.cache_data(ttl=3600, show_spinner=False)
def get_students_by_section(section: str) -> pd.DataFrame:
    section = str(section).strip().upper()
    docs = db.collection('setup_students')\
        .where('Section', '==', section)\
        .where('Status', '==', 'Active')\
        .stream()
    data = [d.to_dict() for d in docs]
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=['USN', 'Name'])
    return df[['USN', 'Name']].sort_values('USN')

@st.cache_data(ttl=300)
def get_pending_papers():
    docs = db.collection('question_papers').where('status', '==', 'Submitted').stream()
    return [d.to_dict() for d in docs]

@st.cache_data(ttl=86400, show_spinner="Calculating attainment...")  # 24h cache
def get_attainment_cached(subject_code: str):
    return calculate_attainment(subject_code)

# ============================= HTML GENERATOR =============================
def generate_qp_html(meta, questions):
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
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Times New Roman', serif; margin: 40px; }}
            .header {{ text-align: center; border-bottom: 3px double black; padding-bottom: 10px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ border: 1px solid black; padding: 8px; text-align: left; }}
            th {{ background-color: #f0f0f0; }}
            .signatures {{ margin-top: 60px; display: flex; justify-content: space-between; }}
            .sig {{ width: 200px; text-align: center; }}
            .line {{ border-top: 1px solid black; margin: 40px 0 5px 0; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>AMC ENGINEERING COLLEGE</h1>
            <h3>(AUTONOMOUS)</h3>
            <h2>Department of {meta.get('department', 'ECE')}</h2>
            <h2>{meta.get('examName', 'Internal Assessment Test')}</h2>
        </div>
        <table style="margin-bottom: 20px; width: 100%;">
            <tr><td><strong>Course:</strong> {meta.get('courseName')} ({meta.get('courseCode')})</td>
                <td style="text-align: right;"><strong>Date:</strong> {meta.get('date')}</td></tr>
            <tr><td><strong>Sem/Sec:</strong> {meta.get('semester')}</td>
                <td style="text-align: right;"><strong>Max Marks:</strong> {meta.get('maxMarks', '50')} | <strong>Duration:</strong> {meta.get('duration')}</td></tr>
        </table>
        <table>
            <thead>
                <tr><th>Q.No</th><th>Question</th><th>CO</th><th>RBT</th><th>Marks</th></tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
        <div class="signatures">
            <div class="sig"><div class="line"></div><br>Course Teacher</div>
            <div class="sig"><div class="line"></div><br>Scrutinized By</div>
            <div class="sig"><div class="line"></div><br>HOD</div>
        </div>
    </body>
    </html>
    """
    return html_content

# ============================= DB OPERATIONS (unchanged but kept) =============================
def save_question_paper(subject_code, exam_type, meta, questions, status="Draft"):
    uid = f"{subject_code}_{exam_type}"
    total = sum(int(q['marks']) for q in questions if q['marks'].isdigit())
    meta['maxMarks'] = total
    data = {"subject_code": subject_code, "exam_type": exam_type, "meta": meta,
            "questions": questions, "status": status, "timestamp": datetime.now().isoformat()}
    db.collection('question_papers').document(uid).set(data)

def fetch_question_paper(subject_code, exam_type):
    doc = db.collection('question_papers').document(f"{subject_code}_{exam_type}").get()
    return doc.to_dict() if doc.exists else None

def approve_paper(subject_code, exam_type):
    db.collection('question_papers').document(f"{subject_code}_{exam_type}").update({"status": "Approved"})

def save_attendance_record(records):
    batch = db.batch()
    for r in records:
        uid = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}".replace(" ","_")
        batch.set(db.collection('attendance_records').document(uid), r)
    batch.commit()

def save_ia_marks(records, exam_type, subject_code):
    batch = db.batch()
    for r in records:
        uid = f"{exam_type}_{subject_code}_{r['USN']}"
        batch.set(db.collection('ia_marks').document(uid), r)
    batch.commit()

def save_copo_mapping(subject_code, mapping_data):
    db.collection('co_po_mappings').document(subject_code).set({"mapping": mapping_data})

def fetch_copo_mapping(subject_code):
    doc = db.collection('co_po_mappings').document(subject_code).get()
    return doc.to_dict()['mapping'] if doc.exists else None

def register_students_bulk(df, ay, batch, semester, section):
    batch = db.batch()
    coll = db.collection('setup_students')
    for _, row in df.iterrows():
        usn = str(row['USN']).strip().upper()
        name = str(row['Name']).strip()
        data = {"USN": usn, "Name": name, "Academic_Year": ay, "Batch": batch,
                "Semester": semester, "Section": section.upper(), "Status": "Active"}
        batch.set(coll.document(usn), data, merge=True)
    batch.commit()

# ============================= ATTAINMENT (unchanged) =============================
def calculate_attainment(subject_code):
    # ← YOUR ORIGINAL 120-LINE calculate_attainment() FUNCTION GOES HERE UNCHANGED →
    # I'm keeping it short here but you paste your full version
    return {"CO": {"CO1": 3, "CO2": 2}, "PO": {"PO1": 2.8}}, "Success"

# ============================= FACULTY DASHBOARD =============================
def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")

    # Get faculty list once
    faculty_list = sorted({d.to_dict().get('Faculty Name','') 
                          for d in db.collection('setup_subjects').stream() 
                          if d.to_dict().get('Faculty Name')})

    if not faculty_list:
        st.error("No subjects found. Ask admin to upload setup_subjects.csv")
        return

    selected_faculty = st.selectbox("Select Your Name", faculty_list)
    df_subjects = get_subjects_by_faculty(selected_faculty)

    if df_subjects.empty:
        st.info("No classes assigned to you.")
        return

    selected_label = st.selectbox("Select Class", df_subjects['Display_Label'].unique())
    row = df_subjects[df_subjects['Display_Label'] == selected_label].iloc[0]
    current_sec = row['Section'].upper()
    current_sub = row['Subject Name']
    current_code = row['Subject Code']

    df_students = get_students_by_section(current_sec)
    st.write(f"**{current_sub} ({current_code}) - {current_sec}** | Active Students: {len(df_students)}")

    tabs = st.tabs(["📝 Attendance", "📄 Question Paper", "💯 IA Marks", "📊 Attainment", "📋 CO-PO Mapping"])

    with tabs[0]:  # Attendance
        c1, c2 = st.columns(2)
        date = c1.date_input("Date", datetime.today())
        slot = c2.selectbox("Slot", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "02:00-03:00"])
        if not df_students.empty:
            df_att = df_students.copy()
            df_att['Present'] = True
            edited = st.data_editor(df_att, hide_index=True)
            if st.button("Submit Attendance", type="primary"):
                recs = [dict(Date=str(date), Time=slot, Faculty=selected_faculty,
                            Section=current_sec, Code=current_code, USN=r['USN'],
                            Status="Present" if r['Present'] else "Absent") 
                        for _, r in edited.iterrows()]
                save_attendance_record(recs)
                st.success("Attendance saved!")
        else:
            st.info("No active students")

    with tabs[1]:  # Question Paper
        exam_type = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="qp")
        qp = fetch_question_paper(current_code, exam_type)
        if qp:
            qs_df = pd.DataFrame(qp['questions'])
            meta = qp['meta']
            st.write(f"Status: **{qp['status']}**")
        else:
            qs_df = pd.DataFrame(columns=['qNo','text','marks','co','bt'])
            meta = {"date": str(datetime.today()), "duration": "90 mins"}

        with st.expander("Exam Details"):
            col1, col2 = st.columns(2)
            meta['date'] = col1.text_input("Date", meta.get('date', ''))
            meta['duration'] = col2.text_input("Duration", meta.get('duration', ''))

        with st.expander("Questions"):
            edited_qs = st.data_editor(qs_df, num_rows="dynamic",
                column_config={
                    "qNo": st.column_config.TextColumn("Q.No"),
                    "text": st.column_config.TextColumn("Question"),
                    "marks": st.column_config.NumberColumn("Marks", min_value=1, max_value=20),
                    "co": st.column_config.SelectboxColumn("CO", options=[f"CO{i}" for i in range(1,7)]),
                    "bt": st.column_config.SelectboxColumn("RBT", options=["L1","L2","L3","L4","L5","L6"])
                })

        meta.update({"examName": exam_type, "courseName": current_sub, "courseCode": current_code,
                     "semester": current_sec, "department": "ECE"})

        if st.button("Preview Paper"):
            html = generate_qp_html(meta, edited_qs.to_dict('records'))
            st.components.v1.html(html, height=800, scrolling=True)

        c1, c2 = st.columns(2)
        if c1.button("Save Draft"): 
            save_question_paper(current_code, exam_type, meta, edited_qs.to_dict('records'), "Draft")
            st.success("Draft saved")
        if c2.button("Submit for Approval"):
            save_question_paper(current_code, exam_type, meta, edited_qs.to_dict('records'), "Submitted")
            st.success("Submitted!")
            st.rerun()

    with tabs[2]:  # IA Marks
        exam = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="marks")
        qp = fetch_question_paper(current_code, exam)
        if not qp or qp['status'] != 'Approved':
            st.error("Question paper not approved yet")
        else:
            marks_df = df_students.copy()
            for q in qp['questions']:
                marks_df[q['qNo']] = 0
            edited = st.data_editor(marks_df, hide_index=True, disabled=["USN","Name"])
            if st.button("Submit Marks", type="primary"):
                recs = []
                for _, r in edited.iterrows():
                    scores = {col: int(r[col]) for col in marks_df.columns if col not in ['USN','Name']}
                    recs.append({"USN": r['USN'], "Name": r['Name'], "Exam": exam,
                                 "Subject": current_sub, "Code": current_code,
                                 "Scores": scores, "Total_Obtained": sum(scores.values())})
                save_ia_marks(recs, exam, current_code)
                st.success("Marks submitted!")

    with tabs[3]:  # Attainment
        if st.button("Generate CO-PO Attainment Report", type="primary"):
            result, msg = get_attainment_cached(current_code)
            if result:
                c1, c2 = st.columns(2)
                with c1:
                    st.subheader("CO Attainment")
                    st.table(pd.DataFrame.from_dict(result['CO'], orient='index', columns=['Level']))
                with c2:
                    st.subheader("PO Attainment")
                    st.table(pd.DataFrame.from_dict(result['PO'], orient='index', columns=['Value']))
            else:
                st.error(msg)

    with tabs[4]:  # CO-PO Mapping
        matrix = fetch_copo_mapping(current_code)
        if not matrix:
            index = [f"CO{i}" for i in range(1,7)]
            cols = [f"PO{i}" for i in range(1,13)] + ["PSO1","PSO2"]
            matrix = pd.DataFrame(0, index=index, columns=cols)
            matrix.insert(0, "CO", index)
        edited = st.data_editor(matrix, hide_index=True)
        if st.button("Save Mapping"):
            save_copo_mapping(current_code, edited.drop(columns=['CO']).to_dict(orient='list'))
            st.success("Saved!")

# ============================= HOD & ADMIN =============================
def render_hod():
    st.subheader("HOD / Scrutiny Board")
    papers = get_pending_papers()
    if not papers:
        st.success("No pending approvals")
        return
    for p in papers:
        with st.expander(f"{p['meta']['courseCode']} - {p['exam_type']}"):
            st.write(f"Date: {p['meta']['date']} | Marks: {p['meta'].get('maxMarks')}")
            if st.button("Preview", key=f"prev_{p['subject_code']}"):
                st.components.v1.html(generate_qp_html(p['meta'], p['questions']), height=700, scrolling=True)
            if st.button("Approve Paper", key=f"app_{p['subject_code']}"):
                approve_paper(p['subject_code'], p['exam_type'])
                st.success("Approved!")
                st.rerun()

def render_admin():
    st.subheader("System Admin")
    tab1, tab2 = st.tabs(["Student Registration", "Master Uploads"])
    with tab1:
        uploaded = st.file_uploader("Upload Students CSV (USN, Name)", type='csv')
        section = st.text_input("Section (e.g., 5A)").upper()
        if uploaded and section and st.button("Register Students"):
            df = pd.read_csv(uploaded)
            register_students_bulk(df, "2025-26", "2022", 5, section)
            st.success(f"Registered {len(df)} students in {section}")

    with tab2:
        sub = st.file_uploader("Upload Subjects CSV", type='csv')
        if sub and st.button("Upload Subjects"):
            df = pd.read_csv(sub)
            for _, r in df.iterrows():
                db.collection('setup_subjects').add(r.to_dict())
            st.success("Subjects uploaded")

# ============================= MAIN =============================
def main():
    st.sidebar.title("RMS v6.3")
    role = st.sidebar.radio("Login As", ["Faculty", "HOD / Scrutiny", "Admin"])
    if role == "Faculty":
        render_faculty_dashboard()
    elif role == "HOD / Scrutiny":
        render_hod()
    else:
        render_admin()

if __name__ == "__main__":
    main()
