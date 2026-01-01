import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime

# ==========================================
# 1. CONFIGURATION & THEME
# ==========================================
st.set_page_config(page_title="RMS Attendance Pro", layout="wide", page_icon="🎓")

# Custom CSS from your 'Base Version Upgraded' file
def load_custom_css():
    st.markdown("""
    <style>
        .stApp { background-color: #f8f9fa; font-family: 'Inter', sans-serif; }
        div[data-testid="stSidebar"] { background-color: #ffffff; border-right: 1px solid #e0e0e0; }
        h1, h2, h3 { color: #2c3e50; font-weight: 700; }
        .stButton>button { border-radius: 8px; font-weight: 600; }
        div[data-testid="stMetricValue"] { font-size: 1.8rem !important; color: #2980b9; }
        .stDataFrame { border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden; }
    </style>
    """, unsafe_allow_html=True)

load_custom_css()

# ==========================================
# 2. FIREBASE CONNECTION
# ==========================================
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
                st.error("Secrets missing."); st.stop()
        except Exception as e:
            st.error(f"DB Error: {e}"); st.stop()
    return firestore.client()

db = get_db()

# ==========================================
# 3. CORE FUNCTIONS (READ/WRITE)
# ==========================================

@st.cache_data(ttl=60)
def fetch_faculty_list():
    """Get list of faculty for login."""
    try:
        docs = db.collection('setup_subjects').stream()
        return sorted(list(set(d.to_dict().get('Faculty Name', '') for d in docs)))
    except: return []

@st.cache_data(ttl=60)
def fetch_subjects(faculty_name):
    """Get subjects assigned to logged-in faculty."""
    try:
        docs = db.collection('setup_subjects').where('Faculty Name', '==', faculty_name).stream()
        df = pd.DataFrame([d.to_dict() for d in docs])
        if not df.empty:
            df['Display'] = df['Section'] + " - " + df['Subject Name']
        return df
    except: return pd.DataFrame()

@st.cache_data(ttl=10)
def fetch_class_students(section):
    """Get active students for marking attendance."""
    try:
        # Fetch all students first (better for small datasets than complex indexes)
        docs = db.collection('setup_students').stream()
        data = [d.to_dict() for d in docs if d.to_dict().get('Section') == section and d.to_dict().get('Status') == 'Active']
        return pd.DataFrame(data).sort_values('USN')
    except: return pd.DataFrame()

@st.cache_data(ttl=5)
def fetch_attendance_data(section, subject_code):
    """Fetch history for analytics."""
    try:
        docs = db.collection('attendance_records')\
                 .where('Section', '==', section)\
                 .where('Code', '==', subject_code).stream()
        return pd.DataFrame([d.to_dict() for d in docs])
    except: return pd.DataFrame()

def save_attendance(records):
    batch = db.batch(); c = 0
    for r in records:
        uid = f"{r['Date']}_{r['Section']}_{r['Code']}_{r['Time']}_{r['USN']}"
        batch.set(db.collection('attendance_records').document(uid), r)
        c+=1
        if c>=400: batch.commit(); batch=db.batch(); c=0
    if c>0: batch.commit()
    fetch_attendance_data.clear()

# ==========================================
# 4. ADMIN PANEL (For Empty DB Setup)
# ==========================================
def render_admin():
    st.title("🛠️ System Admin")
    st.info("Since your DB is empty, start by uploading Subjects, then Students.")
    
    tab1, tab2 = st.tabs(["📚 1. Setup Subjects", "👨‍🎓 2. Setup Students"])
    
    with tab1:
        st.write("Upload CSV with columns: `Faculty Name`, `Subject Name`, `Subject Code`, `Section`")
        up = st.file_uploader("Subjects CSV", type=['csv'], key='sub_up')
        if up and st.button("Upload Subjects"):
            df = pd.read_csv(up)
            batch = db.batch(); c=0
            for i, r in df.iterrows():
                batch.set(db.collection('setup_subjects').document(str(i)), r.to_dict())
                c+=1
            batch.commit()
            st.success(f"Uploaded {len(df)} subjects!")
            fetch_faculty_list.clear()

    with tab2:
        st.write("Upload CSV with columns: `USN`, `Name`")
        c1,c2,c3 = st.columns(3)
        ay = c1.text_input("Academic Year", "2024-25")
        sem = c2.selectbox("Semester", [1,2,3,4,5,6,7,8])
        sec = c3.text_input("Section", "A")
        
        up2 = st.file_uploader("Students CSV", type=['csv'], key='stu_up')
        if up2 and st.button("Register Students"):
            df = pd.read_csv(up2)
            batch = db.batch(); c=0
            for _, r in df.iterrows():
                data = r.to_dict()
                data.update({"AY": ay, "Sem": sem, "Section": sec, "Status": "Active"})
                batch.set(db.collection('setup_students').document(str(r['USN'])), data)
                c+=1
                if c>=400: batch.commit(); batch=db.batch(); c=0
            if c>0: batch.commit()
            st.success(f"Registered {len(df)} students!")

# ==========================================
# 5. FACULTY DASHBOARD (The Core App)
# ==========================================
def render_faculty(user):
    st.title(f"👨‍🏫 Welcome, {user}")
    
    # Select Subject
    subs = fetch_subjects(user)
    if subs.empty: st.warning("No subjects assigned."); return
    
    c1, c2 = st.columns([3, 1])
    sel_sub = c1.selectbox("Select Class", subs['Display'].unique())
    
    # Extract Context
    curr = subs[subs['Display'] == sel_sub].iloc[0]
    sec, code, sub_name = curr['Section'], curr['Subject Code'], curr['Subject Name']
    
    t1, t2, t3 = st.tabs(["📝 Mark Attendance", "📉 Shortage Analytics", "📅 View History"])
    
    # --- TAB 1: MARKING ---
    with t1:
        c1, c2 = st.columns(2)
        date = c1.date_input("Date")
        time_slot = c2.selectbox("Slot", ["09:00-10:00", "10:00-11:00", "11:15-12:15", "02:00-03:00"])
        
        st.divider()
        students = fetch_class_students(sec)
        
        if not students.empty:
            # Grid Input
            students['Present'] = True
            edited = st.data_editor(students[['USN', 'Name', 'Present']], hide_index=True, use_container_width=True, height=500)
            
            if st.button("💾 Submit Attendance", type="primary", use_container_width=True):
                recs = []
                for _, row in edited.iterrows():
                    recs.append({
                        "Date": str(date), "Time": time_slot, "Faculty": user,
                        "Section": sec, "Code": code, "Subject": sub_name,
                        "USN": row['USN'], "Name": row['Name'],
                        "Status": "Present" if row['Present'] else "Absent",
                        "Timestamp": datetime.now().isoformat()
                    })
                save_attendance(recs)
                st.toast("Attendance Saved!", icon="✅")
        else:
            st.error("No students found. Ask Admin to upload.")

    # --- TAB 2: ANALYTICS (Grok Feature) ---
    with t2:
        st.subheader("Attendance Report & Shortage List")
        hist = fetch_attendance_data(sec, code)
        
        if not hist.empty:
            total_classes = hist['Date'].nunique()
            st.metric("Total Classes Conducted", total_classes)
            
            # Logic: Calculate % for every student
            all_stu = fetch_class_students(sec)
            res = []
            
            for _, s in all_stu.iterrows():
                attended = len(hist[(hist['USN'] == s['USN']) & (hist['Status'] == 'Present')])
                perc = (attended / total_classes * 100) if total_classes > 0 else 0
                res.append({"USN": s['USN'], "Name": s['Name'], "Attended": attended, "%": round(perc, 1)})
            
            df_res = pd.DataFrame(res)
            
            # Styling: Red if < 75%
            def color_shortage(val):
                color = '#ffdddd' if val < 75 else '#ddffdd'
                return f'background-color: {color}; color: black'

            st.dataframe(df_res.style.applymap(color_shortage, subset=['%']), use_container_width=True)
        else:
            st.info("No attendance records found yet.")

    # --- TAB 3: HISTORY ---
    with t3:
        if not hist.empty:
            d_filter = st.date_input("Filter Date", key="hist_d")
            day_recs = hist[hist['Date'] == str(d_filter)]
            if not day_recs.empty:
                st.dataframe(day_recs[['Time', 'USN', 'Name', 'Status']], use_container_width=True, hide_index=True)
            else:
                st.warning("No records for this date.")

# ==========================================
# 6. LOGIN SYSTEM
# ==========================================
def main():
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
    
    if not st.session_state.logged_in:
        c1, c2, c3 = st.columns([1,2,1])
        with c2:
            st.markdown("## 🔐 Login")
            role = st.selectbox("Role", ["Faculty", "Admin"])
            
            user_input = ""
            if role == "Faculty":
                facs = fetch_faculty_list()
                if facs: user_input = st.selectbox("Select Name", facs)
                else: st.warning("DB Empty. Login as Admin to setup."); role="Admin"
            
            pwd = st.text_input("Password", type="password")
            
            if st.button("Login", use_container_width=True):
                # Simple Auth using Secrets
                admin_pw = st.secrets["general"]["admin_password"]
                fac_pw = st.secrets["general"]["faculty_password"]
                
                valid = False
                if role == "Admin" and pwd == admin_pw: valid = True; user_input = "Administrator"
                elif role == "Faculty" and pwd == fac_pw: valid = True
                
                if valid:
                    st.session_state.logged_in = True
                    st.session_state.user = user_input
                    st.session_state.role = role
                    st.rerun()
                else:
                    st.error("Invalid Credentials")
    else:
        # Sidebar Logout
        with st.sidebar:
            st.write(f"Logged in as: **{st.session_state.user}**")
            if st.button("Logout"):
                st.session_state.logged_in = False; st.rerun()
        
        if st.session_state.role == "Admin": render_admin()
        else: render_faculty(st.session_state.user)

if __name__ == "__main__":
    main()
