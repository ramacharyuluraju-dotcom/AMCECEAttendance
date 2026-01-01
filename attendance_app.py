import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Attendance System", layout="centered", page_icon="📝")

# --- FIREBASE CONNECTION (Robust Fix) ---
@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        try:
            # 1. Check if 'textkey' exists in secrets
            if "textkey" in st.secrets:
                secret_val = st.secrets["textkey"]
                
                # 2. robust handling: If it's a string (from the foolproof method), parse it.
                # If it's already a dict (from standard toml), use it as is.
                if isinstance(secret_val, str):
                    key_dict = json.loads(secret_val)
                else:
                    key_dict = secret_val

                cred = credentials.Certificate(key_dict)
                firebase_admin.initialize_app(cred)
            else:
                st.error("Error: 'textkey' missing in .streamlit/secrets.toml")
                st.stop()
        except Exception as e:
            st.error(f"Firebase Connection Error: {e}")
            st.stop()
    return firestore.client()

db = get_db()

# ============================= AUTHENTICATION =============================

def check_login(username, password, role):
    try:
        # Get passwords from secrets, defaulting to simple ones if missing
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
        # Fetch all students and filter in python to avoid index issues
        docs = db.collection('setup_students').stream()
        data = [d.to_dict() for d in docs if str(d.to_dict().get('Section')).upper() == section]
        
        if not data: return pd.DataFrame(columns=['USN', 'Name'])
        
        df = pd.DataFrame(data)
        if 'Status' in df.columns:
            df = df[df['Status'] == 'Active']
        return df[['USN', 'Name']].sort_values('USN')
    except:
        return pd.DataFrame(columns=['USN', 'Name'])

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
    df_subs = get_subjects_for_faculty(user_name)
    
    if df_subs.empty:
        st.warning("No subjects assigned to you.")
        return

    sel_cls = st.selectbox("Select Class", df_subs['Display_Label'].unique())
    cls_info = df_subs[df_subs['Display_Label'] == sel_cls].iloc[0]
    
    c1, c2 = st.columns(2)
    d_val = c1.date_input("Date", datetime.now())
    t_slot = c2.selectbox("Time Slot", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "01:00-02:00", "02:00-03:00", "03:00-04:00"])

    df_students = get_active_students_in_section(cls_info['Section'].upper())
    
    if not df_students.empty:
        att_df = df_students.copy()
        att_df['Present'] = True
        edited_df = st.data_editor(att_df, column_config={"Present": st.column_config.CheckboxColumn(default=True)}, disabled=["USN","Name"], hide_index=True, use_container_width=True, height=400)

        if st.button("💾 Submit Attendance", type="primary", use_container_width=True):
            recs = []
            for _, row in edited_df.iterrows():
                recs.append({
                    "Date": str(d_val), "Time": t_slot, "Faculty": user_name,
                    "Section": cls_info['Section'].upper(), "Code": cls_info['Subject Code'],
                    "Subject": cls_info['Subject Name'], "USN": row['USN'],
                    "Name": row['Name'], "Status": "Present" if row['Present'] else "Absent",
                    "Timestamp": datetime.now().isoformat()
                })
            save_attendance_record(recs)
            st.success("Attendance Saved!")
    else:
        st.error("No students found.")

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
