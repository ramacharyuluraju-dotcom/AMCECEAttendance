import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Attendance System", layout="centered", page_icon="📝")

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

# ============================= AUTHENTICATION =============================

def check_login(username, password, role):
    # In a production app, fetch password hashes from Firestore.
    # Here we use secrets or defaults for simplicity.
    try:
        if role == "Admin":
            sys_pass = st.secrets["general"]["admin_password"]
        else:
            sys_pass = st.secrets["general"]["faculty_password"]
        
        if password == sys_pass:
            st.session_state['logged_in'] = True
            st.session_state['user'] = username
            st.session_state['role'] = role
            st.rerun()
        else:
            st.error("Incorrect Password")
    except KeyError:
        st.error("Passwords not set in secrets.toml")

def logout():
    st.session_state['logged_in'] = False
    st.session_state['user'] = None
    st.session_state['role'] = None
    st.rerun()

# ============================= FETCH FUNCTIONS =============================

@st.cache_data(ttl=3600) 
def get_all_faculty_names():
    """Fetch unique faculty names for dropdown."""
    try:
        docs = db.collection('setup_subjects').stream()
        return sorted(list(set(d.to_dict().get('Faculty Name', '') for d in docs if d.to_dict().get('Faculty Name'))))
    except Exception:
        return []

@st.cache_data(ttl=3600) 
def get_subjects_for_faculty(faculty_name):
    """Fetch subjects assigned to a specific faculty."""
    if not faculty_name: return pd.DataFrame()
    try:
        all_docs = db.collection('setup_subjects').stream()
        data = [d.to_dict() for d in all_docs if d.to_dict().get('Faculty Name') == faculty_name.strip()]
        df = pd.DataFrame(data)
        if not df.empty:
            df['Display_Label'] = df['Section'].astype(str).str.upper() + " - " + df['Subject Name']
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=60) # Short cache for students so updates reflect quickly
def get_active_students_in_section(section):
    """Fetch active students for a specific section."""
    section = str(section).strip().upper()
    try:
        docs = db.collection('setup_students').where('Section', '==', section).stream()
        data = [d.to_dict() for d in docs]
        
        # Fallback query if 'where' fails due to indexing
        if not data:
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
    """Batch write attendance to Firestore."""
    batch = db.batch()
    count = 0
    for r in records:
        # Unique ID: Date_Section_Code_Time_USN
        uid = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}".replace(" ","").replace("/","-")
        batch.set(db.collection('attendance_records').document(uid), r)
        count += 1
        if count >= 450: # batch limit safety
            batch.commit()
            batch = db.batch()
            count = 0
    if count > 0:
        batch.commit()

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
    get_all_faculty_names.clear() # Clear cache
    return len(records)

def register_students_bulk(df, ay, batch_yr, sem, sec):
    records = df.to_dict(orient='records')
    batch = db.batch(); c = 0
    coll = db.collection('setup_students')
    for r in records:
        usn = str(r['USN']).strip().upper()
        data = {"USN": usn, "Name": str(r['Name']).strip(), "AY": ay, "Batch": batch_yr, "Sem": sem, "Section": sec, "Status": "Active"}
        batch.set(coll.document(usn), data, merge=True)
        c += 1
        if c >= 400: batch.commit(); batch = db.batch(); c=0
    if c > 0: batch.commit()
    get_active_students_in_section.clear() # Clear cache
    return c

# ============================= UI VIEWS =============================

def login_screen():
    st.markdown("<h1 style='text-align: center;'>🔐 Login</h1>", unsafe_allow_html=True)
    
    role = st.radio("Select Role", ["Faculty", "Admin"], horizontal=True)
    
    with st.form("login_form"):
        if role == "Faculty":
            # Fetch faculty names for easier login
            fac_names = get_all_faculty_names()
            username = st.selectbox("Select Name", fac_names) if fac_names else st.text_input("Username")
        else:
            username = "Administrator"
            
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In")
        
        if submitted:
            check_login(username, password, role)

def render_attendance_interface(user_name):
    st.subheader(f"👋 Welcome, {user_name}")
    
    # 1. Select Subject
    df_subs = get_subjects_for_faculty(user_name)
    
    if df_subs.empty:
        st.warning("No subjects assigned to you. Please contact Admin.")
        return

    c1, c2 = st.columns([2, 1])
    sel_cls = c1.selectbox("Select Class / Subject", df_subs['Display_Label'].unique())
    
    # Extract details based on selection
    cls_info = df_subs[df_subs['Display_Label'] == sel_cls].iloc[0]
    cur_sec = cls_info['Section'].upper()
    cur_code = cls_info['Subject Code']
    cur_sub = cls_info['Subject Name']

    # 2. Select Date/Time
    c1, c2 = st.columns(2)
    d_val = c1.date_input("Date", datetime.now())
    t_slot = c2.selectbox("Time Slot", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "01:00-02:00", "02:00-03:00", "03:00-04:00"])

    st.divider()

    # 3. Fetch Students & Mark Attendance
    df_students = get_active_students_in_section(cur_sec)
    
    if df_students.empty:
        st.error(f"No active students found in Section {cur_sec}")
    else:
        st.info(f"Marking for: **{cur_sub}** | Sec: **{cur_sec}** | Count: **{len(df_students)}**")
        
        # Prepare dataframe for editor
        att_df = df_students.copy()
        att_df['Present'] = True # Default to present
        
        # Data Editor (Checkboxes)
        edited_df = st.data_editor(
            att_df,
            column_config={
                "Present": st.column_config.CheckboxColumn("Present?", help="Uncheck for Absent", default=True)
            },
            disabled=["USN", "Name"],
            hide_index=True,
            use_container_width=True,
            height=400
        )

        # Submit Button
        if st.button("💾 Submit Attendance", type="primary", use_container_width=True):
            with st.spinner("Saving records..."):
                records = []
                for _, row in edited_df.iterrows():
                    status = "Present" if row['Present'] else "Absent"
                    records.append({
                        "Date": str(d_val),
                        "Time": t_slot,
                        "Faculty": user_name,
                        "Section": cur_sec,
                        "Subject": cur_sub,
                        "Code": cur_code,
                        "USN": row['USN'],
                        "Name": row['Name'],
                        "Status": status,
                        "Timestamp": datetime.now().isoformat()
                    })
                save_attendance_record(records)
            st.success("✅ Attendance Saved Successfully!")

def render_admin_panel():
    st.subheader("🛠️ Admin Panel")
    
    tab1, tab2 = st.tabs(["📚 Upload Subjects", "🎓 Register Students"])

    with tab1:
        st.write("Upload the Subject/Faculty Allocation CSV.")
        up_sub = st.file_uploader("Upload Subjects CSV", type=['csv'])
        if st.button("Process Subject List") and up_sub:
            try:
                count = upload_subjects(pd.read_csv(up_sub))
                st.success(f"Successfully uploaded {count} subject allocations.")
            except Exception as e:
                st.error(f"Error: {e}")

    with tab2:
        st.write("Register new batch of students.")
        c1, c2, c3 = st.columns(3)
        ay = c1.text_input("Academic Year", "2024-25")
        sem = c2.selectbox("Semester", [1,2,3,4,5,6,7,8])
        sec = c3.text_input("Section", "A")
        
        up_stu = st.file_uploader("Upload Students CSV", type=['csv'])
        if st.button("Register Students") and up_stu:
            try:
                c = register_students_bulk(pd.read_csv(up_stu), ay, str(datetime.now().year), sem, sec)
                st.success(f"Successfully registered {c} students.")
            except Exception as e:
                st.error(f"Error: {e}")

# ============================= MAIN APP =============================

def main():
    # Initialize Session State
    if 'logged_in' not in st.session_state:
        st.session_state['logged_in'] = False
        st.session_state['user'] = None
        st.session_state['role'] = None

    # Sidebar Navigation
    if st.session_state['logged_in']:
        with st.sidebar:
            st.title("Attendance App")
            st.write(f"User: **{st.session_state['user']}**")
            st.write(f"Role: **{st.session_state['role']}**")
            st.divider()
            if st.button("Log Out"):
                logout()
        
        # Route based on Role
        if st.session_state['role'] == "Admin":
            render_admin_panel()
        else:
            render_attendance_interface(st.session_state['user'])
            
    else:
        login_screen()

if __name__ == "__main__":
    main()
