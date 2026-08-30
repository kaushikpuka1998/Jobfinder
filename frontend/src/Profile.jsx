import { useEffect, useState } from "react";
import { api } from "./api.js";

const SIMPLE_FIELDS = [
  ["first_name", "First name"], ["last_name", "Last name"],
  ["email", "Email"], ["phone", "Phone"], ["location", "Location"],
  ["linkedin", "LinkedIn"], ["github", "GitHub"], ["portfolio", "Portfolio"],
  ["twitter", "Twitter / X"], ["current_company", "Current company"],
  ["current_title", "Current title"], ["work_authorisation", "Work authorisation"],
  ["notice_period", "Notice period"], ["salary_expectation", "Salary expectation"],
];
const ADDRESS_FIELDS = [
  ["address_line_1", "Address line 1"], ["address_line_2", "Address line 2"],
  ["city", "City"], ["state", "State / Province"],
  ["postal_code", "Postal code"], ["country", "Country"],
];

const EDU_COLS = [["school", "School / University"], ["degree", "Degree"],
                  ["field_of_study", "Field of study"], ["gpa", "GPA / result"],
                  ["start", "From (YYYY)"], ["end", "To (YYYY)"]];
const EXP_COLS = [["company", "Company"], ["title", "Job title"],
                  ["location", "Location"], ["start", "From (MM/YYYY)"],
                  ["end", "To (MM/YYYY or Present)"],
                  ["description", "Role description", "area"]];
const WEB_COLS = [["url", "URL", true]];
const AP_COLS = { education: EDU_COLS, experience: EXP_COLS, websites: WEB_COLS };
const AP_TITLES = { education: "Education", experience: "Work experience", websites: "Websites" };
const MAX_ENTRIES = 10;

function EntryList({ listName, entries, onChange }) {
  const cols = AP_COLS[listName];
  const title = AP_TITLES[listName];

  function update(idx, key, value) {
    const next = entries.slice();
    next[idx] = { ...next[idx], [key]: value };
    onChange(next);
  }
  function remove(idx) {
    onChange(entries.filter((_, i) => i !== idx));
  }
  function add() {
    if (entries.length >= MAX_ENTRIES) return;
    onChange([...entries, Object.fromEntries(cols.map(([k]) => [k, ""]))]);
  }

  return (
    <>
      {entries.length ? entries.map((v, idx) => (
        <div className="entry" key={idx}>
          <div className="entry-head">
            <b>{title} {idx + 1}</b>
            <a href="#" onClick={e => { e.preventDefault(); remove(idx); }}>Remove</a>
          </div>
          <div className="grid2">
            {cols.map(([key, label, full]) => (
              <div className={full ? "span2" : ""} key={key}>
                <label>{label}</label>
                {full === "area"
                  ? <textarea rows="2" value={v[key] || ""} onChange={e => update(idx, key, e.target.value)} />
                  : <input type="text" value={v[key] || ""} onChange={e => update(idx, key, e.target.value)} />}
              </div>
            ))}
          </div>
        </div>
      )) : <div className="hint">None yet.</div>}
      <button className="ghost" style={{ marginTop: 8 }} onClick={add}>+ Add {title.toLowerCase()}</button>
    </>
  );
}

export default function Profile() {
  const [screening, setScreening] = useState([]);
  const [fields, setFields] = useState({});
  const [lists, setLists] = useState({ education: [], experience: [], websites: [] });
  const [err, setErr] = useState("");
  const [status, setStatus] = useState("");
  const [bm, setBm] = useState(null);
  const [insp, setInsp] = useState(null);
  const [copyLabel, setCopyLabel] = useState("Copy link");

  useEffect(() => {
    api("/api/meta").then(d => setScreening(d.screening)).catch(() => {});
    load();
  }, []);

  async function load() {
    try {
      const d = await api("/api/applicant");
      const a = d.applicant;
      setFields(a);
      setLists({ education: a.education || [], experience: a.experience || [], websites: a.websites || [] });
      refreshBookmarklet();
    } catch (e) { setErr(e.message); }
  }

  async function refreshBookmarklet() {
    try { setBm(await api("/api/bookmarklet")); } catch (e) { setBm(null); return; }
    try { setInsp(await api("/api/inspector")); } catch (e) { setInsp(null); }
  }

  function setField(key, value) {
    setFields(f => ({ ...f, [key]: value }));
  }

  async function save() {
    setErr(""); setStatus("");
    const body = { ...lists };
    for (const [k] of SIMPLE_FIELDS) body[k] = (fields[k] || "").trim();
    for (const [k] of ADDRESS_FIELDS) body[k] = (fields[k] || "").trim();
    body.heard_about_us = (fields.heard_about_us || "").trim();
    body.skills = (fields.skills || "").trim();
    for (const [key] of screening) body[key] = fields[key] || "";
    try {
      const d = await api("/api/applicant", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setFields(d.applicant);
      setLists({ education: d.applicant.education || [], experience: d.applicant.experience || [],
                 websites: d.applicant.websites || [] });
      setStatus("saved");
      refreshBookmarklet();
    } catch (e) { setErr(e.message); }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(bm.bookmarklet);
      setCopyLabel("Copied — paste over the old bookmark's URL");
      setTimeout(() => setCopyLabel("Copy link"), 4000);
    } catch (e) { setCopyLabel("Copy failed — drag the button instead"); }
  }

  return (
    <>
      <header>
        <h1>Apply kit</h1>
        <span className="sub">Your details for autofilling application forms —
          saved locally to <code>applicant.json</code></span>
        <a className="btnlink ghostlink" href="/" style={{ marginLeft: "auto" }}>← Back to jobs</a>
      </header>
      <main>
        <div className="formgrid">
          <div className="card">
            <h2>Personal</h2>
            <div className="hint" style={{ margin: "0 0 12px" }}>Your details, saved locally to{" "}
              <code>applicant.json</code>. The bookmarklet fills these into whatever
              application form you're looking at. <b>It never submits</b> — you check
              the values and press Submit yourself.</div>
            <div className="grid2">
              {SIMPLE_FIELDS.map(([key, label]) => (
                <div key={key}>
                  <label>{label}</label>
                  <input type="text" value={fields[key] || ""} onChange={e => setField(key, e.target.value)} />
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h2>Address</h2>
            <div className="grid2">
              {ADDRESS_FIELDS.map(([key, label]) => (
                <div key={key}>
                  <label>{label}</label>
                  <input type="text" value={fields[key] || ""} onChange={e => setField(key, e.target.value)} />
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h2>Other</h2>
            <label>How did you hear about us?</label>
            <input type="text" value={fields.heard_about_us || ""}
                   onChange={e => setField("heard_about_us", e.target.value)} placeholder="LinkedIn" />
            <label>Skills (comma separated)</label>
            <input type="text" value={fields.skills || ""}
                   onChange={e => setField("skills", e.target.value)} placeholder="Python, Django, AWS" />
          </div>

          <div className="card">
            <h2>Education</h2>
            <EntryList listName="education" entries={lists.education}
                       onChange={v => setLists(l => ({ ...l, education: v }))} />
          </div>

          <div className="card">
            <h2>Work experience</h2>
            <EntryList listName="experience" entries={lists.experience}
                       onChange={v => setLists(l => ({ ...l, experience: v }))} />
          </div>

          <div className="card">
            <h2>Screening questions</h2>
            {screening.map(([key, label]) => (
              <div className="qrow" key={key}>
                <label>{label}</label>
                <select value={fields[key] || ""} onChange={e => setField(key, e.target.value)}>
                  <option value="">— leave blank —</option>
                  <option value="Yes">Yes</option>
                  <option value="No">No</option>
                </select>
              </div>
            ))}
          </div>

          <div className="card">
            <h2>Websites</h2>
            <EntryList listName="websites" entries={lists.websites}
                       onChange={v => setLists(l => ({ ...l, websites: v }))} />
          </div>

          <div className="card">
            <div className="row">
              <button onClick={save}>Save details</button>
              <span className="muted">{status}</span>
            </div>
            <div className="err">{err}</div>

            <div style={{ marginTop: 14 }}>
              {!bm || !bm.filled ? (
                <div className="hint">Fill in your details and save to get the autofill button.</div>
              ) : (
                <>
                  <div className="hint" style={{ marginBottom: 7 }}>
                    <b>Drag this to your bookmarks bar</b>, then click it on any application
                    page to fill the form. Resume uploads and Submit stay yours.<br />
                    <b style={{ color: "var(--warn)" }}>Replace the old bookmark whenever you save</b>
                    {" "}— your details and the matching logic are baked into it. It reports
                    build <code>{bm.build || "?"}</code> when it runs; if the popup shows
                    a different build, you clicked a stale bookmark.
                  </div>
                  <a className="bm" href={bm.bookmarklet} title="Drag me to your bookmarks bar"
                     onClick={e => { e.preventDefault();
                       alert("Drag this button to your bookmarks bar. Then, on any application "
                           + "page, click the bookmark to fill your details in."); }}>
                    ↧ Fill this application
                  </a>
                  <button className="ghost" style={{ marginLeft: 8 }} onClick={copyLink}>{copyLabel}</button>
                  {insp && (
                    <a className="bm" href={insp.bookmarklet}
                       style={{ background: "var(--panel2)", color: "var(--fg)",
                                border: "1px solid var(--line)", marginLeft: 8 }}
                       onClick={e => { e.preventDefault();
                         alert("Drag this to your bookmarks bar too. On a form that fills wrongly, "
                             + "click it: it copies a description of the form's fields (names and "
                             + "labels only — no values) so the mismatch can be diagnosed."); }}>
                      ⌕ Inspect form
                    </a>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      </main>
    </>
  );
}
