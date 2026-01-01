import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Attendance System", layout="wide", page_icon="📝")

# --- FIREBASE CONNECTION ---
@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        try:
            if "textkey" in st.secrets:
                secret_val = st.secrets["textkey"]
                # Handle both string (foolproof) and dict formats
                if isinstance(secret_val, str):
                    key_dict = json.loads(secret_val)
                else:
                    key_dict = secret_val

                cred = credentials.Certificate(key_dict)
                firebase_admin.initialize_app(cred)
            else:
                st.error("Error: 'textkey' missing in secrets.toml")
                st.stop()
        except Exception as e:
            st.error(f"Firebase Connection Error: {e}")
            st.stop()
    return firestore.client()

db = get_db()

# ============================= AUTHENTICATION =============================

def check_login(username, password, role):
    try:
        admin_pass = st.secrets.get("general", {}).get("admin_password", "admin123")
        fac_pass = st.secrets.get("general", {}).get("faculty_password", "1234")
        
        target_pass = admin_pass if role == "Admin" else fac_pass
        
        if password == target_pass:
            st.session_state['logged_in'] = True
            st.session_state['user'] = username
            st.session_state['role'] = role
            st.rerun()
        else:
            st.error("Incorrect Password")
    except Exception as e:
        st.error(f"Login Error: {e}")

def logout():
    st.session_state.clear()
    st.rerun()

# ============================= FETCH FUNCTIONS =============================

@st.cache_data(ttl=3600) 
def get_all_faculty_names():
    try:
        docs = db.collection('setup_subjects').stream()
        return sorted(list(set(d.to_dict().get('Faculty Name', '') for d in docs if d.to_dict().get('Faculty Name'))))
    except: return []

@st.cache_data(ttl=3600) 
def get_subjects_for_faculty(faculty_name):
    if not faculty_name: return pd.DataFrame()
    try:
        all_docs = db.collection('setup_subjects').stream()
        data = [d.to_dict() for d in all_docs if d.to_dict().get('Faculty Name') == faculty_name.strip()]
        df = pd.DataFrame(data)
        if not df.empty:
            df['Display_Label'] = df['Section'].astype(str).str.upper() + " - " + df['Subject Name']
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=60) 
def get_active_students_in_section(section):
    section = str(section).strip().upper()
    try:
        docs = db.collection('setup_students').stream()
        data = [d.to_dict() for d in docs if str(d.to_dict().get('Section')).upper() == section]
        
        if not data: return pd.DataFrame(columns=['USN', 'Name'])
        
        df = pd.DataFrame(data)
        if 'Status' in df.columns:
            df = df[df['Status'] == 'Active']
        return df[['USN', 'Name']].sort_values('USN')
    except:
        return pd.DataFrame(columns=['USN', 'Name'])

# --- NEW: ANALYTICS FETCH FUNCTION ---
@st.cache_data(ttl=10)
def fetch_attendance_history(section, subject_code):
    """Fetches all attendance records for a specific class to calculate stats."""
    try:
        # Note: Ideally requires a composite index in Firebase, but works for small datasets without it
        docs = db.collection('attendance_records')\
                 .where('Section', '==', section)\
                 .where('Code', '==', subject_code)\
                 .stream()
        return [d.to_dict() for d in docs]
    except Exception as e:
        return []

# ============================= WRITE FUNCTIONS =============================

def save_attendance_record(records):
    batch = db.batch()
    count = 0
    for r in records:
        uid = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}".replace(" ","").replace("/","-")
        batch.set(db.collection('attendance_records').document(uid), r)
        count += 1
        if count >= 400: 
            batch.commit()
            batch = db.batch()
            count = 0
    if count > 0: batch.commit()
    # Clear cache so analytics update immediately
    fetch_attendance_history.clear()

# ============================= ADMIN FUNCTIONS =============================

def upload_subjects(df):
    records = df.to_dict(orient='records')
    batch = db.batch(); c = 0
    coll = db.collection('setup_subjects')
    for i, r in enumerate(records):
        doc_ref = coll.document(str(i))
        batch.set(doc_ref, r)
        c += 1
        if c >= 400: batch.commit(); batch = db.batch(); c=0
    if c > 0: batch.commit()
    get_all_faculty_names.clear()
    return len(records)

def register_students_bulk(df, ay, sem, sec):
    records = df.to_dict(orient='records')
    batch = db.batch(); c = 0
    coll = db.collection('setup_students')
    for r in records:
        usn = str(r['USN']).strip().upper()
        data = {"USN": usn, "Name": str(r['Name']).strip(), "AY": ay, "Sem": sem, "Section": sec, "Status": "Active"}
        batch.set(coll.document(usn), data, merge=True)
        c += 1
        if c >= 400: batch.commit(); batch = db.batch(); c=0
    if c > 0: batch.commit()
    get_active_students_in_section.clear()
    return c

# ============================= UI VIEWS =============================

def login_screen():
    st.markdown("<h1 style='text-align: center;'>🔐 Login</h1>", unsafe_allow_html=True)
    role = st.radio("Select Role", ["Faculty", "Admin"], horizontal=True)
    
    with st.form("login_form"):
        if role == "Faculty":
            fac_names = get_all_faculty_names()
            username = st.selectbox("Select Name", fac_names) if fac_names else st.text_input("Username")
        else:
            username = "Administrator"
            
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign In"):
            check_login(username, password, role)

def render_attendance_interface(user_name):
    st.subheader(f"👋 Welcome, {user_name}")
    
    # 1. CLASS SELECTION
    df_subs = get_subjects_for_faculty(user_name)
    if df_subs.empty:
        st.warning("No subjects assigned.")
        return

    c_sel1, c_sel2 = st.columns([3, 1])
    sel_cls = c_sel1.selectbox("Select Class", df_subs['Display_Label'].unique())
    
    cls_info = df_subs[df_subs['Display_Label'] == sel_cls].iloc[0]
    curr_sec = cls_info['Section'].upper()
    curr_code = cls_info['Subject Code']
    curr_sub = cls_info['Subject Name']

    # --- TABS FOR FEATURES ---
    tab1, tab2, tab3 = st.tabs(["📝 Mark Attendance", "📊 Analytics & Report", "📅 History Log"])

    # --- TAB 1: MARK ATTENDANCE ---
    with tab1:
        c1, c2 = st.columns(2)
        d_val = c1.date_input("Date", datetime.now())
        t_slot = c2.selectbox("Time Slot", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "01:00-02:00", "02:00-03:00", "03:00-04:00"])

        df_students = get_active_students_in_section(curr_sec)
        
        if not df_students.empty:
            att_df = df_students.copy()
            att_df['Present'] = True
            
            # Form wrapper to prevent accidental reloads
            with st.form("attendance_form"):
                edited_df = st.data_editor(
                    att_df, 
                    column_config={"Present": st.column_config.CheckboxColumn(default=True)}, 
                    disabled=["USN","Name"], 
                    hide_index=True, 
                    use_container_width=True, 
                    height=400
                )
                submit_btn = st.form_submit_button("💾 Submit Attendance", type="primary")

            if submit_btn:
                recs = []
                for _, row in edited_df.iterrows():
                    recs.append({
                        "Date": str(d_val), "Time": t_slot, "Faculty": user_name,
                        "Section": curr_sec, "Code": curr_code,
                        "Subject": curr_sub, "USN": row['USN'],
                        "Name": row['Name'], "Status": "Present" if row['Present'] else "Absent",
                        "Timestamp": datetime.now().isoformat()
                    })
                save_attendance_record(recs)
                st.success("Attendance Saved Successfully!")
        else:
            st.error("No students found in this section.")

    # --- TAB 2: ANALYTICS (The "Grok" Features) ---
    with tab2:
        st.markdown(f"**Attendance Summary: {curr_sub} ({curr_sec})**")
        
        # 1. Fetch Data
        history_data = fetch_attendance_history(curr_sec, curr_code)
        
        if history_data:
            df_hist = pd.DataFrame(history_data)
            
            # 2. Process Statistics
            total_classes = df_hist['Date'].nunique() # Count unique dates (or date+time combinations)
            
            # Group by USN to count "Present"
            # Filter only where Status == Present
            present_counts = df_hist[df_hist['Status'] == 'Present'].groupby('USN').size()
            
            # Get list of all students (even those with 0 attendance)
            all_students = get_active_students_in_section(curr_sec)
            
            if not all_students.empty:
                report_data = []
                for _, stu in all_students.iterrows():
                    p_count = present_counts.get(stu['USN'], 0)
                    percentage = (p_count / total_classes * 100) if total_classes > 0 else 0
                    report_data.append({
                        "USN": stu['USN'],
                        "Name": stu['Name'],
                        "Total Classes": total_classes,
                        "Attended": p_count,
                        "Percentage": round(percentage, 1)
                    })
                
                df_report = pd.DataFrame(report_data)

                # 3. Highlighting Logic (Red if < 75%)
                def highlight_low_attendance(val):
                    color = '#ffcccc' if val < 75 else '#ccffcc'
                    return f'background-color: {color}'

                st.dataframe(
                    df_report.style.applymap(highlight_low_attendance, subset=['Percentage']),
                    use_container_width=True,
                    hide_index=True
                )
                
                # Download Button
                st.download_button(
                    "📥 Download Report (CSV)",
                    df_report.to_csv(index=False),
                    f"Attendance_{curr_sec}_{curr_code}.csv",
                    "text/csv"
                )
            else:
                st.info("No active students.")
        else:
            st.info("No attendance records found yet.")

    # --- TAB 3: HISTORY LOG ---
    with tab3:
        # Simple date filter
        hist_date = st.date_input("Filter by Date", key="hist_date")
        history_data = fetch_attendance_history(curr_sec, curr_code)
        
        if history_data:
            df_hist = pd.DataFrame(history_data)
            # Filter by selected date
            df_day = df_hist[df_hist['Date'] == str(hist_date)]
            
            if not df_day.empty:
                st.write(f"Records for {hist_date}: {len(df_day)} entries")
                # Pivot for cleaner view if multiple slots exist
                st.dataframe(df_day[['Time', 'USN', 'Name', 'Status']], hide_index=True, use_container_width=True)
            else:
                st.warning(f"No classes recorded on {hist_date}")

def render_admin_panel():
    st.subheader("🛠️ Admin Panel")
    t1, t2 = st.tabs(["Upload Subjects", "Register Students"])

    with t1:
        st.info("CSV Columns: 'Faculty Name', 'Subject Name', 'Subject Code', 'Section'")
        up_sub = st.file_uploader("Subjects CSV", type=['csv'])
        if st.button("Upload Subjects") and up_sub:
            upload_subjects(pd.read_csv(up_sub))
            st.success("Uploaded")

    with t2:
        st.info("CSV Columns: 'USN', 'Name'")
        c1, c2, c3 = st.columns(3)
        ay = c1.text_input("AY", "2024-25"); sem = c2.selectbox("Sem", [1,2,3,4,5,6,7,8]); sec = c3.text_input("Sec", "A")
        up_stu = st.file_uploader("Students CSV", type=['csv'])
        if st.button("Register") and up_stu:
            register_students_bulk(pd.read_csv(up_stu), ay, sem, sec)
            st.success("Registered")

# ============================= MAIN =============================

if __name__ == "__main__":
    if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False
    
    if st.session_state['logged_in']:
        with st.sidebar:
            st.write(f"User: **{st.session_state.get('user')}**")
            if st.button("Log Out"): logout()
        
        if st.session_state['role'] == "Admin": render_admin_panel()
        else: render_attendance_interface(st.session_state['user'])
    else:
        login_screen()
