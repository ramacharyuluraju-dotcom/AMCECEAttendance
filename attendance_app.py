import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Dept. Record Management System", layout="wide", page_icon="🎓")

# --- FIREBASE CONNECTION (Singleton) ---
@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        try:
            if "textkey" in st.secrets:
                secret_val = st.secrets["textkey"]
                key_dict = json.loads(secret_val) if isinstance(secret_val, str) else secret_val
                cred = credentials.Certificate(key_dict)
                firebase_admin.initialize_app(cred)
            else:
                st.error("Firebase credentials missing in Secrets.")
                st.stop()
        except Exception as e:
            st.error(f"Firebase init failed: {e}")
            st.stop()
    return firestore.client()

db = get_db()

# ============================= FETCH FUNCTIONS =============================

@st.cache_data(ttl=86400) # Cache Faculty List for 24 Hours
def get_all_faculty_names():
    """Reads all subjects ONCE a day."""
    try:
        docs = db.collection('setup_subjects').stream()
        # Use simple string key access
        return sorted(list(set(d.to_dict().get('Faculty Name', '') for d in docs if d.to_dict().get('Faculty Name'))))
    except Exception as e:
        return []

@st.cache_data(ttl=3600) # Cache Subject List for 1 Hour
def get_subjects_for_faculty(faculty_name):
    if not faculty_name: return pd.DataFrame()
    try:
        # Standard query - Firestore handles spaces in strings fine usually
        docs = db.collection('setup_subjects').where('Faculty Name', '==', faculty_name.strip()).stream()
        data = [d.to_dict() for d in docs]
        df = pd.DataFrame(data)
        if not df.empty:
            df['Display_Label'] = df['Section'].astype(str).str.upper() + " - " + df['Subject Name']
        return df
    except Exception:
        # Fallback: Fetch all and filter in Python (1 read per doc, but safe)
        all_docs = db.collection('setup_subjects').stream()
        data = [d.to_dict() for d in all_docs if d.to_dict().get('Faculty Name') == faculty_name.strip()]
        df = pd.DataFrame(data)
        if not df.empty:
            df['Display_Label'] = df['Section'].astype(str).str.upper() + " - " + df['Subject Name']
        return df

@st.cache_data(ttl=3600) # Cache Student List
def get_active_students_in_section(section):
    section = str(section).strip().upper()
    try:
        docs = db.collection('setup_students').where('Section', '==', section).stream()
        data = [d.to_dict() for d in docs]
        if not data: return pd.DataFrame(columns=['USN', 'Name'])
        df = pd.DataFrame(data)
        if 'Status' in df.columns:
            df = df[df['Status'] == 'Active']
        return df[['USN', 'Name']].sort_values('USN')
    except:
        return pd.DataFrame(columns=['USN', 'Name'])

@st.cache_data(ttl=60)
def get_pending_question_papers():
    try:
        docs = db.collection('question_papers').where('status', '==', 'Submitted').stream()
        return [d.to_dict() for d in docs]
    except: return []

@st.cache_data(ttl=60)
def fetch_question_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    doc = db.collection('question_papers').document(uid).get()
    return doc.to_dict() if doc.exists else None

@st.cache_data(ttl=300)
def fetch_copo_mapping(subject_code):
    doc = db.collection('co_po_mappings').document(subject_code).get()
    return doc.to_dict()['mapping'] if doc.exists else None

# --- WRITE OPS ---

def safe_write(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
        return True
    except Exception as e:
        if "Quota" in str(e): st.error("Quota Exceeded.")
        else: st.error(f"Error: {e}")
        return False

def save_attendance_record(records):
    batch = db.batch()
    for r in records:
        uid = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}".replace(" ","").replace("/","-")
        batch.set(db.collection('attendance_records').document(uid), r)
    batch.commit()

def save_ia_marks(records, exam_type, subject_code):
    batch = db.batch()
    for r in records:
        uid = f"{exam_type}_{subject_code}_{r['USN']}".replace(" ","")
        batch.set(db.collection('ia_marks').document(uid), r)
    batch.commit()

def save_question_paper(subject_code, exam_type, meta, questions, status="Draft"):
    uid = f"{subject_code}_{exam_type}"
    total = sum(int(q['marks']) for q in questions if str(q['marks']).isdigit())
    meta['maxMarks'] = total
    data = {"subject_code": subject_code, "exam_type": exam_type, "meta": meta, "questions": questions, "status": status, "timestamp": datetime.now().isoformat()}
    db.collection('question_papers').document(uid).set(data)
    fetch_question_paper.clear()
    if status == 'Submitted': get_pending_question_papers.clear()

def approve_paper(subject_code, exam_type):
    uid = f"{subject_code}_{exam_type}"
    db.collection('question_papers').document(uid).update({"status": "Approved"})
    fetch_question_paper.clear()
    get_pending_question_papers.clear()

def save_copo_mapping(subject_code, mapping_data):
    db.collection('co_po_mappings').document(subject_code).set({"mapping": mapping_data})
    fetch_copo_mapping.clear()

# --- UTILS ---
def generate_qp_html(meta, questions):
    rows = "".join([f"<tr><td style='text-align:center'>{q['qNo']}</td><td>{q['text']}</td><td style='text-align:center'>{q['co']}</td><td style='text-align:center'>{q['bt']}</td><td style='text-align:center'>{q['marks']}</td></tr>" for q in questions])
    return f"""<html><body style='font-family:serif;padding:20px'><div style='text-align:center;border-bottom:2px solid black'><h2>AMC ENGINEERING COLLEGE</h2><h4>Dept of {meta.get('department','ECE')} | {meta.get('examName')}</h4></div><br><div style='display:flex;justify-content:space-between'><div><b>Course:</b> {meta.get('courseName')} ({meta.get('courseCode')})<br><b>Sem:</b> {meta.get('semester')}</div><div style='text-align:right'><b>Date:</b> {meta.get('date')}<br><b>Max Marks:</b> {meta.get('maxMarks')}</div></div><br><table border='1' style='width:100%;border-collapse:collapse'><tr><th>Q.No</th><th>Question</th><th>CO</th><th>RBT</th><th>Marks</th></tr>{rows}</table></body></html>"""

# --- ADMIN FUNCTIONS ---
def upload_to_firestore(collection_name, df):
    records = df.to_dict(orient='records')
    batch = db.batch(); c = 0
    coll = db.collection(collection_name)
    for i, r in enumerate(records):
        doc_ref = coll.document(str(i))
        batch.set(doc_ref, r)
        c += 1
        if c >= 400: batch.commit(); batch = db.batch(); c=0
    if c > 0: batch.commit()
    get_all_faculty_names.clear()
    return i+1

def register_students_bulk(df, ay, batch_yr, sem, sec):
    records = df.to_dict(orient='records')
    batch = db.batch(); c = 0
    coll = db.collection('setup_students')
    for r in records:
        usn = str(r['USN']).strip().upper()
        data = {"USN": usn, "Name": str(r['Name']).strip(), "AY": ay, "Batch": batch_yr, "Sem": sem, "Section": sec, "Status": "Active"}
        # Use USN as Doc ID for easier management
        batch.set(coll.document(usn), data, merge=True)
        c += 1
        if c >= 400: batch.commit(); batch = db.batch(); c=0
    if c > 0: batch.commit()
    get_active_students_in_section.clear()
    return c

def update_student_status(usn, status):
    doc_ref = db.collection('setup_students').document(usn)
    if doc_ref.get().exists:
        doc_ref.update({"Status": status})
        get_active_students_in_section.clear()
        return True
    return False

def delete_collection(coll_ref, limit=50):
    docs = coll_ref.limit(limit).stream()
    d = 0
    for doc in docs: doc.reference.delete(); d+=1
    if d >= limit: delete_collection(coll_ref, limit)

# --- REPORT ENGINE ---
def calculate_attainment(subject_code):
    try:
        marks_ref = db.collection('ia_marks').where('Code', '==', subject_code).stream()
        marks_data = [d.to_dict() for d in marks_ref]
        if not marks_data: return None, "No marks found."
        
        exam_types = set(m['Exam'] for m in marks_data)
        patterns = {}
        for et in exam_types:
            qp = fetch_question_paper(subject_code, et)
            if qp and qp.get('status') == 'Approved':
                patterns[et] = {str(q['qNo']): {'co': q['co'], 'max': int(q['marks'])} for q in qp['questions']}
        
        if not patterns: return None, "No Approved QP found."

        stu_scores = {}
        for r in marks_data:
            ex, usn, sc = r['Exam'], r['USN'], r['Scores']
            if ex not in patterns: continue
            pat = patterns[ex]
            if usn not in stu_scores: 
                stu_scores[usn] = {f"CO{i}":0 for i in range(1,7)}; stu_scores[usn].update({f"CO{i}_max":0 for i in range(1,7)})
            for q, m in sc.items():
                if q in pat:
                    co, mx = pat[q]['co'], pat[q]['max']
                    stu_scores[usn][co] += m
                    stu_scores[usn][f"{co}_max"] += mx

        co_res = {}
        for co in [f"CO{i}" for i in range(1,7)]:
            passed = sum(1 for d in stu_scores.values() if d[f"{co}_max"]>0 and (d[co]/d[f"{co}_max"]*100)>=60)
            total = len(stu_scores)
            perc = (passed/total*100) if total else 0
            co_res[co] = 3 if perc>=70 else 2 if perc>=60 else 1 if perc>=50 else 0

        copo = fetch_copo_mapping(subject_code)
        po_res = {}
        if copo:
            for po, vals in copo.items():
                if po=="CO_ID": continue
                w_sum=0; count=0
                for i, v in enumerate(vals):
                    co_k = f"CO{i+1}"
                    v_int = int(v) if str(v).isdigit() else 0
                    if v_int>0 and co_k in co_res: w_sum+=v_int*co_res[co_k]; count+=v_int
                po_res[po] = round(w_sum/count, 2) if count>0 else 0
                
        return {"CO": co_res, "PO": po_res}, "Success"
    except Exception as e:
        return None, str(e)

# --- UI RENDERING ---

def render_faculty_dashboard():
    st.subheader("👨‍🏫 Faculty Dashboard")
    
    faculty_names = get_all_faculty_names()
    if not faculty_names: st.error("System Empty. Contact Admin."); return
    
    sel_fac = st.selectbox("Faculty", faculty_names)
    df_subs = get_subjects_for_faculty(sel_fac)
    
    if df_subs.empty: st.info("No classes."); return
    
    sel_cls = st.selectbox("Class", df_subs['Display_Label'].unique())
    cls_info = df_subs[df_subs['Display_Label'] == sel_cls].iloc[0]
    cur_sec = cls_info['Section'].upper()
    cur_sub = cls_info['Subject Name']
    cur_code = cls_info['Subject Code']
    
    df_students = get_active_students_in_section(cur_sec)
    
    st.write(f"Active Students: {len(df_students)}")
    st.divider()
    
    t1, t2, t3, t4, t5 = st.tabs(["Attendance", "QP Setter", "IA Marks", "CO-PO", "Reports"])
    
    with t1:
        c1, c2 = st.columns(2)
        d_val = c1.date_input("Date")
        t_slot = c2.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "02:00-03:00"])
        if not df_students.empty:
            att = df_students.copy(); att['Present'] = True
            edt = st.data_editor(att, hide_index=True)
            if st.button("Submit"):
                recs = [{"Date":str(d_val), "Time":t_slot, "Faculty":sel_fac, "Section":cur_sec, "Code":cur_code, "USN":r['USN'], "Status":"Present" if r['Present'] else "Absent"} for _,r in edt.iterrows()]
                save_attendance_record(recs); st.success("Saved!")
        else: st.warning("No students in section.")

    with t2:
        etype = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="qp")
        eqp = fetch_question_paper(cur_code, etype)
        status = eqp.get('status', 'New') if eqp else 'New'
        st.info(f"Status: {status}")
        
        df_q = pd.DataFrame(eqp['questions']) if eqp else pd.DataFrame({"qNo":["1a"],"text":[""],"marks":[0],"co":["CO1"],"bt":["L1"]})
        meta = eqp['meta'] if eqp else {"date": str(datetime.now().date()), "duration": "90 Mins"}
        
        c1, c2 = st.columns(2)
        md = c1.text_input("Date", meta.get('date'), key="md"); dur = c2.text_input("Dur", meta.get('duration'), key="mr")
        edt_q = st.data_editor(df_q, num_rows="dynamic", use_container_width=True)
        
        q_list = edt_q.to_dict('records')
        meta_new = {"examName":etype, "courseName":cur_sub, "courseCode":cur_code, "semester":cur_sec, "date":md, "duration":dur}
        
        c1, c2, c3 = st.columns(3)
        if c1.button("Preview"): st.components.v1.html(generate_qp_html(meta_new, q_list), height=500, scrolling=True)
        if c2.button("Save Draft"): safe_write(save_question_paper, cur_code, etype, meta_new, q_list, "Draft"); st.success("Saved")
        if c3.button("Submit HOD"): safe_write(save_question_paper, cur_code, etype, meta_new, q_list, "Submitted"); st.success("Sent!"); st.rerun()

    with t3:
        ie = st.selectbox("Exam", ["IA Test 1", "IA Test 2", "IA Test 3"], key="ia")
        qp = fetch_question_paper(cur_code, ie)
        if not qp or qp.get('status') != "Approved": st.warning("QP Approved Required.")
        elif not df_students.empty:
            q_cols = [q['qNo'] for q in qp['questions']]
            m_df = df_students.copy(); 
            for c in q_cols: m_df[c] = 0
            edt_m = st.data_editor(m_df, disabled=["USN","Name"], hide_index=True)
            if st.button("Save Marks"):
                recs = []
                for _,r in edt_m.iterrows():
                    sc = {c: r[c] for c in q_cols}
                    recs.append({"USN":r['USN'], "Name":r['Name'], "Exam":ie, "Subject":cur_sub, "Code":cur_code, "Scores":sc, "Total":sum(sc.values())})
                safe_write(save_ia_marks, recs, ie, cur_code); st.success("Saved")

    with t4:
        # CSV Upload Feature for CO-PO
        with st.expander("⬆️ Upload CSV"):
            up_cp = st.file_uploader("CO-PO CSV", key="cp_up")
            if up_cp:
                try:
                    df_up = pd.read_csv(up_cp).fillna(0)
                    # Simplified mapping logic for upload
                    safe_write(save_copo_mapping, cur_code, df_up.to_dict('list'))
                    st.success("Uploaded!"); st.rerun()
                except: st.error("Invalid CSV")
        
        # Grid
        cols = [f"PO{i}" for i in range(1,13)]+["PSO1","PSO2"]; rows = [f"CO{i}" for i in range(1,7)]
        ex_map = fetch_copo_mapping(cur_code)
        df_cp = pd.DataFrame(ex_map) if ex_map else pd.DataFrame(0, index=rows, columns=cols)
        if not ex_map: df_cp.insert(0, "CO_ID", rows)
        edt_cp = st.data_editor(df_cp, hide_index=True)
        if st.button("Save Mapping"): safe_write(save_copo_mapping, cur_code, edt_cp.to_dict('list')); st.success("Saved")

    with t5:
        if st.button("Generate Report"):
            res, msg = calculate_attainment(cur_code)
            if res:
                c1, c2 = st.columns(2)
                c1.dataframe(pd.DataFrame(list(res['CO'].items()), columns=['CO','Lvl']), hide_index=True)
                c2.dataframe(pd.DataFrame(list(res['PO'].items()), columns=['PO','Val']), hide_index=True)
            else: st.error(msg)

def render_hod_scrutiny():
    st.subheader("HOD")
    pen = get_pending_question_papers()
    if not pen: st.info("No pending papers."); return
    for p in pen:
        with st.expander(f"{p['meta']['courseCode']} - {p['exam_type']}"):
            if st.button(f"Approve {p['meta']['courseCode']}"): approve_paper(p['subject_code'], p['exam_type']); st.success("Approved!"); st.rerun()

def render_admin_space():
    st.subheader("Admin")
    t1, t2, t3 = st.tabs(["Register", "Uploads", "Manage"])
    
    # 1. REGISTER
    with t1:
        c1, c2, c3 = st.columns(3)
        ay = c1.selectbox("AY", ["24-25"]); sem = c2.selectbox("Sem", [1,2,3,4,5,6,7,8]); sec = c3.text_input("Sec", "A")
        up = st.file_uploader("Student CSV", type=['csv'])
        if st.button("Register") and up: 
            c = register_students_bulk(pd.read_csv(up), ay, "2023", sem, sec); st.success(f"Added {c}")
    
    # 2. UPLOADS (Subject Upload & Wipe DB)
    with t2:
        up_sub = st.file_uploader("Subjects CSV")
        if st.button("Upload") and up_sub: upload_to_firestore('setup_subjects', pd.read_csv(up_sub)); st.success("Done")
        
        st.divider()
        with st.expander("⚠️ Danger Zone"):
            if st.button("🗑️ Wipe DB"): 
                delete_collection(db.collection('setup_subjects'), 50)
                delete_collection(db.collection('setup_students'), 50)
                st.success("Wiped")

    # 3. MANAGE (Search & Update Status) - RESTORED
    with t3:
        q = st.text_input("Search USN").strip().upper()
        if q:
            doc = db.collection('setup_students').document(q).get()
            if doc.exists:
                d = doc.to_dict()
                st.write(f"**{d.get('Name')}** | Status: **{d.get('Status')}**")
                ns = st.selectbox("Status", ["Active", "Detained"])
                if st.button("Update"): 
                    update_student_status(q, ns)
                    st.success("Updated")
            else: st.warning("Not Found")

def main():
    st.sidebar.title("RMS v6.3")
    menu = st.sidebar.radio("Role", ["Faculty Dashboard", "HOD / Scrutiny", "System Admin"])
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "HOD / Scrutiny": render_hod_scrutiny()
    elif menu == "System Admin": render_admin_space()

if __name__ == "__main__":
    main()
