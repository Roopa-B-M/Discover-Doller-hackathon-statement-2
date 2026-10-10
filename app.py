"""ShiftSwap Intelligence - Streamlit UI. All rules/enforcement live in services.py."""
import pandas as pd
import streamlit as st

import eligibility as elig
import services as svc
from database import resolve_db_path
from seed_loader import SeedFileNotFoundError, initialize

st.set_page_config(page_title="ShiftSwap Intelligence", page_icon="🩺", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 1200px;}
.pill {display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.78rem; font-weight:600;}
.pill-open {background:#E3F2FD; color:#0D47A1;} .pill-claimed {background:#FFF3E0; color:#E65100;}
.pill-approved {background:#E8F5E9; color:#1B5E20;} .pill-withdrawn {background:#ECEFF1; color:#455A64;}
.ok {color:#1B5E20; font-weight:600;} .bad {color:#B71C1C; font-weight:600;} .na {color:#757575;}
</style>
""", unsafe_allow_html=True)

DB = resolve_db_path()
try:
    initialize(DB)
except SeedFileNotFoundError as exc:
    st.error(str(exc))
    st.stop()


# ----------------------------------------------------------------- helpers
def fmt_when(sh):
    s, e = elig.parse_dt(sh["start"]), elig.parse_dt(sh["end"])
    end_txt = f"{e:%H:%M}" + ("" if s.date() == e.date() else " (+1 day)")
    return f"{s:%a %d %b %Y}, {s:%H:%M}-{end_txt}"


def pill(status):
    return f'<span class="pill pill-{status.lower()}">{status}</span>'


def flash(result, alt_for=None):
    st.session_state["flash"] = {"result": result, "alt_for": alt_for}


def show_flash(me):
    data = st.session_state.pop("flash", None)
    if not data:
        return
    res = data["result"]
    (st.success if res["success"] else st.error)(res["message"])
    for reason in res["reasons"]:
        st.markdown(f"- ❌ {reason}")
    if not res["success"] and data.get("alt_for") and me["role"] == "STAFF":
        st.markdown("**Other open shifts you can claim right now:**")
        render_alternatives(me, exclude=data["alt_for"], key_prefix="flash")


def act(fn, *args, alt_for=None):
    flash(fn(*args, db_path=DB), alt_for)
    st.rerun()


def render_checklist(checklist):
    for item in checklist:
        icon = {True: "✅", False: "❌", None: "⏭️"}[item["passed"]]
        cls = {True: "ok", False: "bad", None: "na"}[item["passed"]]
        st.markdown(f'{icon} <span class="{cls}">Rule {item["rule"]} - {item["title"]}</span>',
                    unsafe_allow_html=True)
        for m in item["messages"]:
            st.caption(m)


def render_workload(wl):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Currently assigned", f"{elig.fmt_hours(wl['current_hours'])} h")
    c2.metric("This shift", f"+{elig.fmt_hours(wl['candidate_hours'])} h")
    c3.metric("Projected", f"{elig.fmt_hours(wl['projected_hours'])} h",
              delta=f"limit {wl['limit']} h", delta_color="off")
    c4.metric("Remaining", f"{elig.fmt_hours(wl['remaining_hours'])} h")
    st.progress(min(max(wl["projected_hours"] / wl["limit"], 0.0), 1.0))
    st.caption(f"Week {wl['week_start']} to {wl['week_end']}: {wl['summary']}")
    if wl["over_limit"]:
        st.error(f"Weekly-hour rule violated: {elig.fmt_hours(wl['projected_hours'])} h > {wl['limit']} h.")


def render_alternatives(me, exclude=None, key_prefix="alt"):
    alts = svc.get_eligible_alternatives(me["id"], exclude, db_path=DB)
    if not alts:
        st.info("No other open shifts are claimable by you right now. Check back later.")
        return
    for a in alts:
        with st.container(border=True):
            st.markdown(f"**{a['shift_id']}** - {fmt_when(a)} - cert: {a['required_certification'] or 'none'}"
                        f" - owner: {a['poster_name']} ({a['poster_id']})")
            st.caption("Why it's eligible: " + a["explanation"])
            if st.button("Claim this shift", key=f"{key_prefix}_claim_{a['listing_id']}"):
                act(svc.claim_listing, me["id"], a["listing_id"], alt_for=a["listing_id"])


def history_table(rows):
    if not rows:
        st.info("No actions recorded yet.")
        return
    df = pd.DataFrame(rows)
    df["actor"] = df["actor_id"] + " " + df["actor_name"]
    df = df[["id", "created_at", "actor", "action", "shift_id", "outcome", "details"]]
    df.columns = ["#", "Time", "Actor", "Action", "Shift", "Outcome", "Details"]
    st.dataframe(df, width="stretch", hide_index=True)


# ----------------------------------------------------------------- sidebar
people = svc.get_people(DB)
labels = {p["id"]: f"{p['id']} - {p['name']}" for p in people}
st.sidebar.title("🩺 ShiftSwap")
st.sidebar.caption("CityCare Clinic - explainable, rule-aware swaps")
me_id = st.sidebar.selectbox("Acting as", list(labels), format_func=labels.get, key="person")
me = next(p for p in people if p["id"] == me_id)
st.sidebar.markdown(f"**{me['name']}** · `{me['id']}` · {me['role']}")
if me["certifications"]:
    st.sidebar.caption("Certifications: " + ", ".join(me["certifications"]))

if me["role"] == "STAFF":
    pages = ["Dashboard", "My Roster", "Browse Shifts", "My Claims", "Workload Preview",
             "Alternative Shifts", "Action History"]
else:
    pages = ["Dashboard", "Approval Queue", "Action History"]
page = st.sidebar.radio("Navigate", pages, key=f"nav_{me['role']}")
st.sidebar.caption(f"Database: {DB}")

st.title(page)
show_flash(me)

# --------------------------------------------------------------- dashboard
if page == "Dashboard":
    c = svc.get_dashboard_counts(DB)
    r1 = st.columns(4)
    r1[0].metric("Staff", c["staff"]); r1[1].metric("Managers", c["managers"])
    r1[2].metric("Shifts", c["shifts"]); r1[3].metric("Actions logged", c["actions"])
    r2 = st.columns(4)
    r2[0].metric("Open listings", c["open"]); r2[1].metric("Awaiting approval", c["claimed"])
    r2[2].metric("Approved swaps", c["approved"]); r2[3].metric("Withdrawn", c["withdrawn"])
    st.caption("All numbers are read live from SQLite. Eligibility is decided by deterministic code.")
    if me["role"] == "STAFF":
        st.subheader("Your weeks")
        for w in svc.get_weekly_workload(me["id"], DB):
            st.write(f"Week of {w['week_start']}: **{elig.fmt_hours(w['hours'])} / {w['limit']} h**")
            st.progress(min(w["hours"] / w["limit"], 1.0))
    else:
        st.info(f"{c['claimed']} claim(s) waiting in the Approval Queue.")

# ------------------------------------------------------------- staff pages
elif page == "My Roster":
    roster = svc.get_staff_roster(me["id"], DB)
    st.caption(f"{len(roster)} assigned shifts")
    if roster:
        df = pd.DataFrame([{
            "Shift": r["shift_id"], "When": fmt_when(r), "Hours": r["hours"],
            "Certification": r["required_certification"] or "-",
            "Listing": r["listing_status"] or "Not listed",
            "Claimed by": r["claimer_id"] or ""} for r in roster])
        st.dataframe(df, width="stretch", hide_index=True)
    colA, colB = st.columns(2)
    with colA:
        st.subheader("Post a shift")
        free = [r for r in roster if not r["listing_status"]]
        with st.form("post_form"):
            pick = st.selectbox("Shift to offer", [r["shift_id"] for r in free],
                                format_func=lambda sid: f"{sid} - " + fmt_when(
                                    next(r for r in free if r["shift_id"] == sid)))
            if st.form_submit_button("Post listing") and pick:
                act(svc.post_listing, me["id"], pick)
    with colB:
        st.subheader("Withdraw a listing")
        mine = [l for l in svc.get_my_listings(me["id"], DB) if l["status"] == "OPEN"]
        if not mine:
            st.info("You have no OPEN listings to withdraw.")
        else:
            with st.form("withdraw_form"):
                pick = st.selectbox("Open listing", [l["listing_id"] for l in mine],
                                    format_func=lambda i: next(
                                        f"#{l['listing_id']} - {l['shift_id']} - " + fmt_when(l)
                                        for l in mine if l["listing_id"] == i))
                if st.form_submit_button("Withdraw"):
                    act(svc.withdraw_listing, me["id"], pick)

elif page == "Browse Shifts":
    open_l = svc.get_open_listings(exclude_poster_id=me["id"], db_path=DB)
    if not open_l:
        st.info("No open listings from other staff right now.")
    for l in open_l:
        a = svc.get_claim_eligibility(me["id"], l["listing_id"], "claim", DB)
        badge = "🟢 Eligible" if a.get("eligible") else "🔴 Not eligible"
        with st.expander(f"{badge} · {l['shift_id']} · {fmt_when(l)} · posted by {l['poster_name']}",
                         expanded=False):
            st.markdown(f"Listing **#{l['listing_id']}** {pill('OPEN')} · certification: "
                        f"**{l['required_certification'] or 'none'}** · owner: {l['poster_name']} "
                        f"({l['poster_id']})", unsafe_allow_html=True)
            if not a.get("available"):
                st.warning(a["message"])
                continue
            st.markdown("**Eligibility checklist**")
            render_checklist(a["checklist"])
            st.markdown("**Workload impact**")
            render_workload(a["workload"])
            if a["eligible"]:
                st.success("Eligible - all applicable rules pass.")
            else:
                st.error("Not eligible - claiming will be refused and recorded.")
                for reason in a["reasons"]:
                    st.markdown(f"- {reason}")
            if st.button("Claim this shift", key=f"claim_{l['listing_id']}"):
                act(svc.claim_listing, me["id"], l["listing_id"], alt_for=l["listing_id"])

elif page == "My Claims":
    claims = svc.get_my_claims(me["id"], DB)
    if not claims:
        st.info("You have not claimed any shifts.")
    for c in claims:
        with st.container(border=True):
            st.markdown(f"**{c['shift_id']}** · {fmt_when(c)} · from {c['poster_name']} · "
                        f"{pill(c['status'])}", unsafe_allow_html=True)
            if c["status"] == "CLAIMED":
                st.caption("Awaiting manager decision. The shift stays with the original owner until approval.")
                if st.button("Unclaim", key=f"unclaim_{c['listing_id']}"):
                    act(svc.unclaim_listing, me["id"], c["listing_id"])
            else:
                st.caption("Approved - this shift is now on your roster.")

elif page == "Workload Preview":
    st.subheader("Weekly assigned hours")
    weeks = svc.get_weekly_workload(me["id"], DB)
    for w in weeks:
        st.write(f"Week {w['week_start']} → {w['week_end']}: **{elig.fmt_hours(w['hours'])} h** "
                 f"of {w['limit']} h ({elig.fmt_hours(w['remaining_hours'])} h left) - {', '.join(w['shifts'])}")
        st.progress(min(w["hours"] / w["limit"], 1.0))
    st.subheader("Preview a claim")
    open_l = svc.get_open_listings(exclude_poster_id=me["id"], db_path=DB)
    if not open_l:
        st.info("No open listings to preview.")
    else:
        pick = st.selectbox("Open listing", [l["listing_id"] for l in open_l],
                            format_func=lambda i: next(f"#{l['listing_id']} {l['shift_id']} - " + fmt_when(l)
                                                       for l in open_l if l["listing_id"] == i))
        a = svc.get_claim_eligibility(me["id"], pick, "claim", DB)
        if a.get("workload"):
            render_workload(a["workload"])
            st.caption("Read-only preview. Everything is re-checked inside the claim transaction.")

elif page == "Alternative Shifts":
    st.write("Open shifts that pass every rule for you right now (computed deterministically).")
    render_alternatives(me, key_prefix="alt")

# ----------------------------------------------------------- manager pages
elif page == "Approval Queue":
    claimed = svc.get_claimed_listings(db_path=DB)
    if not claimed:
        st.info("No claims are waiting for approval.")
    for l in claimed:
        with st.expander(f"{l['shift_id']} · {fmt_when(l)} · {l['poster_name']} → {l['claimer_name']}",
                         expanded=True):
            left, right = st.columns(2)
            left.markdown(f"**Original owner:** {l['poster_name']} ({l['poster_id']})  \n"
                          f"**Proposed new owner:** {l['claimer_name']} ({l['claimer_id']})")
            right.markdown(f"**Shift:** {l['shift_id']} · {fmt_when(l)}  \n"
                           f"**Required certification:** {l['required_certification'] or 'none'}")
            a = svc.get_claim_eligibility(l["claimer_id"], l["listing_id"], "approval", DB)
            if a.get("available"):
                st.markdown("**Current eligibility (Rules 2-5, against the claimer's current assignments)**")
                render_checklist(a["checklist"])
                render_workload(a["workload"])
                if a["eligible"]:
                    st.success("Safe to approve: all rules currently pass.")
                else:
                    st.error("Approval will be refused: " + "; ".join(a["reasons"]))
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Approve", key=f"approve_{l['listing_id']}", type="primary"):
                act(svc.approve_listing, me["id"], l["listing_id"])
            if b2.button("Reject", key=f"reject_{l['listing_id']}"):
                act(svc.reject_listing, me["id"], l["listing_id"])

# ----------------------------------------------------------------- history
elif page == "Action History":
    mine_only = st.checkbox("Only my actions", value=False)
    history_table(svc.get_action_history(me["id"] if mine_only else None, newest_first=True, db_path=DB))
