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

    html_content = f"""
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
    return html_content

# --- DATABASE OPERATIONS ---

def delete_collection(coll_ref, batch_size):
    """Deletes all documents in a collection."""
    docs = coll_ref.limit(batch_size).stream()
    deleted = 0
    for doc in docs:
        doc.reference.delete()
        deleted += 1
    if deleted >= batch_size:
        return delete_collection(coll_ref, batch_size)

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
    return True

def fetch_question_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    doc = db.collection('question_papers').document(uid).get()
    if doc.exists:
        return doc.to_dict()
    return None

def fetch_pending_papers():
    docs = db.collection('question_papers').where('status', '==', 'Submitted').stream()
    return [d.to_dict() for d in docs]

def approve_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    db.collection('question_papers').document(uid).update({"status": "Approved"})

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
    return i + 1

def fetch_collection_as_df(col):
    docs = db.collection(col).stream()
    data = [doc.to_dict() for doc in docs]
    return pd.DataFrame(data) if data else pd.DataFrame()

def save_attendance_record(records):
    """
    STRICT DUPLICATE PREVENTION LOGIC
    It checks using a deterministic ID. If ID exists, it overwrites (updates).
    """
    batch = db.batch()
    collection_ref = db.collection('attendance_records')
    
    for r in records:
        # Create a deterministic ID: DATE_SECTION_CODE_TIME_USN
        # This ensures that for a specific class slot, a student can have ONLY ONE record.
        unique_id = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}"
        unique_id = unique_id.replace(" ", "").replace("/", "-").replace(":", "")
        
        doc_ref = collection_ref.document(unique_id)
        # Using .set() with the same ID will overwrite the existing doc, preventing duplicates
        batch.set(doc_ref, r)
        
    batch.commit()
    # Explicitly clear cache to show new data immediately
    st.cache_data.clear()

def save_ia_marks(records, exam_type, subject_code):
    batch = db.batch()
    for r in records:
        uid = f"{exam_type}_{subject_code}_{r['USN']}".replace(" ","")
        batch.set(db.collection('ia_marks').document(uid), r)
    batch.commit()

def save_copo_mapping(subject_code, mapping_data):
    db.collection('co_po_mappings').document(subject_code).set({"mapping": mapping_data})

def fetch_copo_mapping(subject_code):
    doc = db.collection('co_po_mappings').document(subject_code).get()
    return doc.to_dict()['mapping'] if doc.exists else None

# --- STUDENT MANAGEMENT FUNCTIONS ---

def register_students_bulk(df, ay, batch, semester, section):
    """Registers students with metadata using USN as ID."""
    records = df.to_dict(orient='records')
    batch_write = db.batch()
    coll_ref = db.collection('setup_students')
    count = 0
    
    for rec in records:
        usn = str(rec['USN']).strip().upper()
        name = str(rec['Name']).strip()
        student_data = {
            "USN": usn, "Name": name, "Academic_Year": ay, "Batch": batch,
            "Semester": semester, "Section": section, "Status": "Active",
            "Last_Updated": datetime.now().strftime("%Y-%m-%d")
        }
        batch_write.set(coll_ref.document(usn), student_data, merge=True)
        count += 1
        if count % 400 == 0:
            batch_write.commit()
            batch_write = db.batch()
    if count > 0: batch_write.commit()
    return count

def update_student_status(usn, status):
    doc_ref = db.collection('setup_students').document(usn)
    if doc_ref.get().exists:
        doc_ref.update({"Status": status})
        return True
    return False

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

    # Selectors
    faculty_list = sorted(df_subjects['Faculty Name'].unique().tolist())
    selected_faculty = st.selectbox("Faculty", faculty_list)
    faculty_data = df_subjects[df_subjects['Faculty Name'] == selected_faculty]
    
    if faculty_data.empty: return
    faculty_data['Display_Label'] = faculty_data['Section'].astype(str) + " - " + faculty_data['Subject Name']
    selected_label = st.selectbox("Class", faculty_data['Display_Label'].unique())
    
    # Context
    class_info = faculty_data[faculty_data['Display_Label'] == selected_label].iloc[0]
    current_sec = str(class_info['Section']).strip()
    current_sub = class_info['Subject Name']
    current_code = class_info['Subject Code']
    
    st.divider()
    tabs = st.tabs(["📝 Attendance", "📄 Question Paper", "💯 IA Entry", "📊 Reports", "📋 CO-PO"])

    # 1. ATTENDANCE
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
                edt = st.data_editor(att, hide_index=True)
                if st.button("Submit Attd"):
                    recs = [{"Date":str(d_val), "Time":t_slot, "Faculty":selected_faculty, "Section":current_sec, "Code":current_code, "USN":r['USN'], "Status":"Present" if r['Present'] else "Absent"} for _,r in edt.iterrows()]
                    save_attendance_record(recs); st.success("Saved!")
            else: st.info("No students.")

    # 2. QUESTION PAPER
    with tabs[1]:
        st.markdown("### 📄 Question Paper Setter")
        exam_type = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="qp_exam")
        
        existing_qp = fetch_question_paper(current_code, exam_type)
        if existing_qp:
            st.info(f"Status: **{existing_qp.get('status', 'Draft')}**")
            current_qs = pd.DataFrame(existing_qp['questions'])
            meta_default = existing_qp['meta']
        else:
            st.info("Status: **New Draft**")
            current_qs = pd.DataFrame({
                "qNo": ["1a", "1b", "2a", "2b"],
                "text": ["Explain...", "Define...", "Compare...", "Calculate..."],
                "marks": [5, 5, 5, 5],
                "co": ["CO1", "CO1", "CO2", "CO2"],
                "bt": ["L1", "L2", "L2", "L3"]
            })
            meta_default = {"date": str(datetime.now().date()), "duration": "90 Mins"}

        with st.expander("1. Exam Details", expanded=True):
            c1, c2 = st.columns(2)
            m_date = c1.text_input("Date", meta_default.get('date'))
            m_dur = c2.text_input("Duration", meta_default.get('duration'))
        
        with st.expander("2. Questions", expanded=True):
            edited_qs = st.data_editor(
                current_qs,
                column_config={
                    "qNo": "Q.No", "text": "Question Text", 
                    "marks": st.column_config.NumberColumn("Marks", max_value=20),
                    "co": st.column_config.SelectboxColumn("CO", options=[f"CO{i}" for i in range(1,7)]),
                    "bt": st.column_config.SelectboxColumn("RBT", options=["L1", "L2", "L3", "L4"])
                },
                num_rows="dynamic", use_container_width=True
            )

        qs_list = edited_qs.to_dict(orient='records')
        meta_data = {"examName": exam_type, "courseName": current_sub, "courseCode": current_code, "semester": current_sec, "date": m_date, "duration": m_dur, "department": "ECE"}
        
        if st.button("👁️ Preview Question Paper"):
            html = generate_qp_html(meta_data, qs_list)
            st.components.v1.html(html, height=500, scrolling=True)
        
        c1, c2 = st.columns(2)
        if c1.button("💾 Save Draft"):
            save_question_paper(current_code, exam_type, meta_data, qs_list, "Draft")
            st.success("Draft Saved.")
            
        if c2.button("🚀 Submit for Approval"):
            save_question_paper(current_code, exam_type, meta_data, qs_list, "Submitted")
            st.success("Submitted to HOD!")
            st.rerun()

    # 3. IA ENTRY
    with tabs[2]:
        st.markdown("### 💯 Marks Entry")
        exam_entry = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="ia_entry")
        qp = fetch_question_paper(current_code, exam_entry)
        
        if not qp: st.error("⚠️ QP not found.")
        elif qp.get('status') != "Approved": st.warning(f"⚠️ QP Status: {qp.get('status')}.")
        else:
            if not df_students.empty:
                df_students['Section'] = df_students['Section'].astype(str).str.strip()
                sec_stu = df_students[df_students['Section'] == current_sec].copy()
                if not sec_stu.empty:
                    q_cols = [q['qNo'] for q in qp['questions']]
                    marks_df = sec_stu[['USN', 'Name']].copy()
                    for c in q_cols: marks_df[c] = 0
                    edited_marks = st.data_editor(marks_df, disabled=["USN", "Name"], hide_index=True)
                    if st.button("Submit Marks"):
                        recs = []
                        for _, row in edited_marks.iterrows():
                            scores = {col: row[col] for col in q_cols}
                            total = sum(scores.values())
                            recs.append({
                                "USN": row['USN'], "Name": row['Name'], "Exam": exam_entry,
                                "Subject": current_sub, "Code": current_code, "Scores": scores, 
                                "Total_Obtained": total, "Timestamp": datetime.now().strftime("%Y-%m-%d")
                            })
                        save_ia_marks(recs, exam_entry, current_code)
                        st.success("Marks Saved!")

    # 4. REPORTS
    with tabs[3]: st.info("Attainment reports use data from Approved Question Papers + IA Marks.")
    
    # 5. CO-PO
    with tabs[4]: 
        st.markdown("### 📋 Course Articulation Matrix (CO-PO)")
        cols = [f"PO{i}" for i in range(1, 13)] + ["PSO1", "PSO2"]
        rows = [f"CO{i}" for i in range(1, 7)]
        existing = fetch_copo_mapping(current_code)
        if existing: df_copo = pd.DataFrame(existing)
        else: df_copo = pd.DataFrame(0, index=rows, columns=cols); df_copo.insert(0, "CO_ID", rows)
        edited_copo = st.data_editor(df_copo, hide_index=True, use_container_width=True)
        if st.button("💾 Save CO-PO Mapping"):
            save_copo_mapping(current_code, edited_copo.to_dict(orient='list')); st.success("Mapping Saved!")

def render_hod_scrutiny():
    st.subheader("🔍 HOD / Scrutiny Board")
    st.markdown("Review and Approve Question Papers.")
    
    pending = fetch_pending_papers()
    if not pending:
        st.info("No pending papers for approval.")
        return
        
    for p in pending:
        with st.expander(f"{p['meta']['courseCode']} - {p['exam_type']} (submitted by Faculty)"):
            st.write(f"**Date:** {p['meta']['date']} | **Max Marks:** {p['meta'].get('maxMarks')}")
            
            # Show Preview
            if st.button(f"👁️ View Paper: {p['meta']['courseCode']}", key=f"v_{p['meta']['courseCode']}"):
                html = generate_qp_html(p['meta'], p['questions'])
                st.components.v1.html(html, height=600, scrolling=True)
            
            if st.button(f"✅ Approve {p['meta']['courseCode']}", key=f"a_{p['meta']['courseCode']}"):
                approve_paper(p['subject_code'], p['exam_type'])
                st.success(f"Approved {p['meta']['courseCode']}!")
                st.rerun()

def render_admin_space():
    st.subheader("⚙️ System Admin & Student Lifecycle")
    
    tabs = st.tabs(["🎓 Student Registration", "🚫 Detain/Manage", "🏫 Master Uploads", "📥 Global Reports"])
    
    # TAB 1: REGISTRATION
    with tabs[0]:
        st.markdown("### 🎓 Register Students for Academic Year")
        c1, c2, c3 = st.columns(3)
        with c1: ay = st.selectbox("Academic Year", ["2023-24", "2024-25", "2025-26"], index=1)
        with c2: batch = st.selectbox("Batch (Joining Year)", ["2021", "2022", "2023", "2024"])
        with c3: sem = st.selectbox("Current Semester", [1, 2, 3, 4, 5, 6, 7, 8], index=2)
        
        target_section = st.text_input("Section to Assign (e.g., 3A, 5B)", placeholder="3A").strip()
        
        st.markdown("#### Option A: Bulk Upload (CSV)")
        up_file = st.file_uploader("Upload Student List CSV", type=['csv'])
        
        if st.button("🚀 Register Batch"):
            if up_file and target_section:
                try:
                    df = pd.read_csv(up_file)
                    df.columns = df.columns.str.strip()
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
    st.sidebar.title("RMS v6.1")
    menu = st.sidebar.radio("Role", ["Faculty Dashboard", "HOD / Scrutiny", "System Admin"])
    
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "HOD / Scrutiny": render_hod_scrutiny()
    elif menu == "System Admin": render_admin_space()

if __name__ == "__main__":
    main()
