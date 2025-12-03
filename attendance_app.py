import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Dept. Record Management System", layout="wide", page_icon="graduation-cap")
st.title("RMS v6.2 – Firestore Optimized")

# --- FIREBASE CONNECTION (unchanged) ---
if not firebase_admin._apps:
    try:
        if "textkey" in st.secrets:
            secret_val = st.secrets["textkey"]
            key_dict = json.loads(secret_val) if isinstance(secret_val, str) else secret_val
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred)
        else:
            st.warning("Firebase credentials not found.")
            st.stop()
    except Exception as e:
        st.error(f"Failed to connect to Firebase: {e}")
        st.stop()

db = firestore.client()

# ============================= OPTIMIZED FETCH FUNCTIONS =============================
@st.cache_data(ttl=3600, show_spinner=False)
def get_subjects_for_faculty(faculty_name: str) -> pd.DataFrame:
    docs = db.collection('setup_subjects')\
             .where('Faculty Name', '==', faculty_name.strip())\
             .stream()
    data = [d.to_dict() for d in docs]
    df = pd.DataFrame(data)
    if not df.empty:
        df['Display_Label'] = df['Section'].astype(str).str.upper() + " - " + df['Subject Name']
    return df

@st.cache_data(ttl=3600, show_spinner=False)
def get_active_students_in_section(section: str) -> pd.DataFrame:
    section = str(section).strip().upper()
    docs = db.collection('setup_students')\
             .where('Section', '==', section)\
             .where('Status', '==', 'Active')\
             .stream()
    data = [d.to_dict() for d in docs]
    df = pd.DataFrame(data)[['USN', 'Name']] if data else pd.DataFrame(columns=['USN', 'Name'])
    return df.sort_values('USN')

@st.cache_data(ttl=300)  # Refresh every 5 min
def get_pending_question_papers():
    docs = db.collection('question_papers')\
             .where('status', '==', 'Submitted')\
             .stream()
    return [d.to_dict() for d in docs]

@st.cache_data(ttl=86400, show_spinner="Calculating attainment...")  # 24-hour cache
def calculate_attainment_cached(subject_code):
    return calculate_attainment(subject_code)

# ============================= KEEP ALL YOUR ORIGINAL FUNCTIONS =============================
# → generate_qp_html, save_question_paper, fetch_question_paper, approve_paper,
# → save_attendance_record, save_ia_marks, save_copo_mapping, fetch_copo_mapping,
# → register_students_bulk, delete_collection, upload_to_firestore → ALL UNCHANGED

# Your original calculate_attainment() — keep 100% as-is
def calculate_attainment(subject_code):
    # ← YOUR FULL ORIGINAL FUNCTION — DO NOT CHANGE →
    marks_ref = db.collection('ia_marks').where('Code', '==', subject_code).stream()
    marks_data = [d.to_dict() for d in marks_ref]
    if not marks_data: return None, "No marks found."
    exam_types = set([m['Exam'] for m in marks_data])
    patterns = {}
    for et in exam_types:
        qp = fetch_question_paper(subject_code, et)
        if qp and qp.get('status') == 'Approved':
            pat_dict = {}
            for q in qp['questions']:
                pat_dict[str(q['qNo'])] = {'co': q['co'], 'max': int(q['marks'])}
            patterns[et] = pat_dict
    if not patterns: return None, "No Approved Question Papers found."
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
    co_attainment_results = {}
    for co in [f"CO{i}" for i in range(1, 7)]:
        total_students = len(student_co_scores)
        if total_students == 0: continue
        students_passed = sum(1 for usn, data in student_co_scores.items()
                              if data[f"{co}_max"] > 0 and (data[co] / data[f"{co}_max"]) * 100 >= 60)
        perc = (students_passed / total_students) * 100
        level = 3 if perc >= 70 else 2 if perc >= 60 else 1 if perc >= 50 else 0
        co_attainment_results[co] = level
    copo_matrix = fetch_copo_mapping(subject_code)
    po_results = {}
    if copo_matrix:
        for po_key, values in copo_matrix.items():
            if po_key == "CO_ID": continue
            weighted_sum = count = 0
            for i, val in enumerate(values):
                co_key = f"CO{i+1}"
                if val and val > 0 and co_key in co_attainment_results:
                    weighted_sum += val * co_attainment_results[co_key]
                    count += val
            po_results[po_key] = round(weighted_sum / count, 2) if count > 0 else 0
    return {"CO": co_attainment_results, "PO": po_results}, "Success"

# ============================= FACULTY DASHBOARD – ONLY READS FIXED =============================
def render_faculty_dashboard():
    st.subheader("Faculty Dashboard")

    # Get faculty list (small read)
    faculty_list = sorted({d.to_dict().get('Faculty Name', '') 
                          for d in db.collection('setup_subjects').stream() 
                          if d.to_dict().get('Faculty Name')})

    selected_faculty = st.selectbox("Faculty", faculty_list or ["No faculty found"])
    if not faculty_list:
        st.error("No subjects uploaded. Ask admin.")
        return

    # OPTIMIZED: Only this faculty's subjects
    df_subjects = get_subjects_for_faculty(selected_faculty)
    if df_subjects.empty:
        st.info("No classes assigned.")
        return

    selected_label = st.selectbox("Class", df_subjects['Display_Label'].unique())
    class_info = df_subjects[df_subjects['Display_Label'] == selected_label].iloc[0]
    current_sec = str(class_info['Section']).strip().upper()
    current_sub = class_info['Subject Name']
    current_code = class_info['Subject Code']

    # OPTIMIZED: Only this section's students
    df_students = get_active_students_in_section(current_sec)

    st.write(f"**{current_sub} ({current_code}) – {current_sec}** | Students: {len(df_students)}")

    tabs = st.tabs(["Attendance", "Question Paper", "IA Entry", "Reports", "CO-PO"])

    with tabs[0]:  # Attendance – unchanged
        c1, c2 = st.columns(2)
        d_val = c1.date_input("Date")
        t_slot = c2.selectbox("Time", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "02:00-03:00"])
        if not df_students.empty:
            att = df_students.copy(); att['Present'] = True
            edt = st.data_editor(att, hide_index=True)
            if st.button("Submit Attendance"):
                recs = [{"Date":str(d_val), "Time":t_slot, "Faculty":selected_faculty,
                         "Section":current_sec, "Code":current_code, "USN":r['USN'],
                         "Status":"Present" if r['Present'] else "Absent"} for _,r in edt.iterrows()]
                save_attendance_record(recs)
                st.success("Saved!")

    with tabs[1]:  # Question Paper – unchanged
        # Your full original QP code here (100% same)
        # Just paste your original block

    with tabs[2]:  # IA Entry – unchanged
        # Your full original IA entry code

    with tabs[3]:  # Reports – NOW CACHED
        st.header("Course Attainment Report")
        if st.button("Generate Report"):
            results, msg = calculate_attainment_cached(current_code)  # ← CACHED
            if results:
                c1, c2 = st.columns(2)
                with c1:
                    st.subheader("CO Attainment Levels")
                    st.dataframe(pd.DataFrame(list(results['CO'].items()), columns=['CO', 'Level']), hide_index=True)
                with c2:
                    st.subheader("Final PO Attainment")
                    st.dataframe(pd.DataFrame(list(results['PO'].items()), columns=['PO', 'Value']), hide_index=True)
                st.success("Report generated!")
            else:
                st.error(msg)

    with tabs[4]:  # CO-PO – unchanged
        # Your full original CO-PO code

# ============================= HOD & ADMIN – ONLY READS FIXED =============================
def render_hod_scrutiny():
    st.subheader("HOD / Scrutiny Board")
    pending = get_pending_question_papers()  # ← CACHED
    if not pending:
        st.info("No pending papers.")
        return
    for p in pending:
        with st.expander(f"{p['meta']['courseCode']} - {p['exam_type']}"):
            st.write(f"Date: {p['meta']['date']} | Marks: {p['meta'].get('maxMarks')}")
            if st.button("View Paper", key=f"v_{p['subject_code']}"):
                st.components.v1.html(generate_qp_html(p['meta'], p['questions']), height=600, scrolling=True)
            if st.button("Approve", key=f"a_{p['subject_code']}"):
                approve_paper(p['subject_code'], p['exam_type'])
                st.success("Approved!")
                st.rerun()

# render_admin_space() — unchanged (already efficient)

# ============================= MAIN =============================
def main():
    st.sidebar.title("RMS v6.2")
    menu = st.sidebar.radio("Role", ["Faculty Dashboard", "HOD / Scrutiny", "System Admin"])
    if menu == "Faculty Dashboard": render_faculty_dashboard()
    elif menu == "HOD / Scrutiny": render_hod_scrutiny()
    elif menu == "System Admin": render_admin_space()

if __name__ == "__main__":
    main()
