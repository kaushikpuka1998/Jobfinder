import { useEffect, useRef, useState } from "react";
import { api, debounce, isAdmin } from "./api.js";

const SOURCE_OPTS = [
  ["s_gh", "greenhouse", "Greenhouse", "(42 boards)"],
  ["s_ab", "ashby", "Ashby", "(42 boards)"],
  ["s_lv", "lever", "Lever", "(9 boards)"],
  ["s_wk", "workable", "Workable", "(20 accounts)"],
  ["s_ro", "remoteok", "Remote OK", ""],
  ["s_wd", "workday", "Workday", "(9 employers)"],
  ["s_li", "linkedin", "LinkedIn", "(slow, rate limited)"],
];
const DEFAULT_ON = new Set(["greenhouse", "ashby", "lever", "workable", "remoteok", "workday"]);
const SOURCE_ABBR = { greenhouse: "GH", ashby: "AB", lever: "LV", workable: "WK",
                      workday: "WD", linkedin: "LI", remoteok: "ROK" };
const PENDING_KEY = "applyPending";

const loadPending = () => { try { return JSON.parse(localStorage.getItem(PENDING_KEY) || "[]"); }
                            catch (e) { return []; } };
const savePending = v => localStorage.setItem(PENDING_KEY, JSON.stringify(v));

function shortTerms(terms) {
  const list = (terms || "").split(",").map(t => t.trim()).filter(Boolean);
  if (!list.length) return "";
  const shown = list.slice(0, 5).join(", ");
  return list.length > 5 ? `${shown} +${list.length - 5}` : shown;
}

function expLabel(j) {
  const lo = j.exp_min, hi = j.exp_max;
  if (lo == null && hi == null) return <span className="muted">—</span>;
  if (lo != null && hi != null) return `${lo}–${hi} yrs`;
  if (lo != null) return `${lo}+ yrs`;
  return `≤${hi} yrs`;
}

export default function Dashboard() {
  const [maxUploadMb, setMaxUploadMb] = useState(25);
  const fileRef = useRef(null);
  const [text, setText] = useState("");
  const [locations, setLocations] = useState("");
  const [rerr, setRerr] = useState("");
  const [rstatus, setRstatus] = useState("");
  const [analysing, setAnalysing] = useState(false);
  const [profile, setProfile] = useState(null);
  const [detected, setDetected] = useState(null);

  const [sources, setSources] = useState(DEFAULT_ON);
  const [remoteOnly, setRemoteOnly] = useState(false);
  const [expMin, setExpMin] = useState("");
  const [expMax, setExpMax] = useState("");
  const [expUnknown, setExpUnknown] = useState(true);
  const [since, setSince] = useState(30);
  const [target, setTarget] = useState(100);
  const [doExport, setDoExport] = useState(true);

  const [run, setRun] = useState({ running: false, log: [] });
  const [progressOpen, setProgressOpen] = useState(false);
  const pollRef = useRef(null);

  const [q, setQ] = useState("");
  const [fsource, setFsource] = useState("");
  const [fmin, setFmin] = useState("");
  const [fexpMin, setFexpMin] = useState("");
  const [fexpMax, setFexpMax] = useState("");
  const [fexpUnknown, setFexpUnknown] = useState(true);
  const [fremote, setFremote] = useState(false);
  const [fstatus, setFstatus] = useState("");
  const [flimit, setFlimit] = useState("300");
  const [jobs, setJobs] = useState([]);
  const [jobsMeta, setJobsMeta] = useState({ total: 0, count: 0 });
  const [jobsErr, setJobsErr] = useState("");

  const [stats, setStats] = useState(null);
  const [statsErr, setStatsErr] = useState("");
  const [files, setFiles] = useState([]);
  const [dberr, setDberr] = useState("");
  const [dbok, setDbok] = useState("");
  const [applicantSummary, setApplicantSummary] = useState("loading…");
  const [pending, setPending] = useState(loadPending());

  useEffect(() => {
    api("/api/meta").then(d => setMaxUploadMb(d.max_upload_mb)).catch(() => {});
    loadJobs(); loadStats(); loadFiles(); loadApplicantSummary(); pollStatus();
    const onFocus = () => setPending(loadPending());
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const t = debounce(loadJobs, 300);
    t();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, fsource, fmin, fexpMin, fexpMax, fexpUnknown, fremote, fstatus, flimit]);

  async function loadJobs() {
    const p = new URLSearchParams({ limit: flimit });
    if (q.trim()) p.set("q", q.trim());
    if (fsource) p.set("source", fsource);
    if (fmin) p.set("min_score", fmin);
    if (fexpMin !== "") p.set("exp_min", fexpMin);
    if (fexpMax !== "") p.set("exp_max", fexpMax);
    p.set("include_unknown_exp", fexpUnknown ? "1" : "0");
    if (fremote) p.set("remote", "1");
    if (fstatus) p.set("status", fstatus);
    try {
      const d = await api("/api/jobs?" + p);
      setJobs(d.jobs); setJobsMeta({ total: d.total, count: d.count }); setJobsErr("");
    } catch (e) { setJobsErr(e.message); }
  }

  async function loadStats() {
    try { setStats(await api("/api/stats")); setStatsErr(""); }
    catch (e) { setStatsErr(e.message); }
  }

  async function loadFiles() {
    try { setFiles((await api("/api/files")).files); } catch (e) { /* ignore */ }
  }

  async function loadApplicantSummary() {
    try {
      const d = await api("/api/applicant");
      const a = d.applicant;
      const filled = Object.keys(a).filter(k => typeof a[k] === "string" && a[k]).length;
      setApplicantSummary(
        `${filled} detail${filled === 1 ? "" : "s"} saved · `
        + `${a.experience.length} job${a.experience.length === 1 ? "" : "s"} · `
        + `${a.education.length} education`);
    } catch (e) { setApplicantSummary(e.message); }
  }

  function pollStatus() {
    if (pollRef.current) return;
    pollRef.current = setInterval(status, 1000);
    status();
  }

  async function status() {
    let d;
    try { d = await api("/api/status"); }
    catch (e) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      setRun(r => ({ ...r, log: [...(r.log || []), e.message] }));
      return;
    }
    const elapsed = d.finished ? d.finished - d.started : (d.started ? (Date.now() / 1000 - d.started) : 0);
    setRun({ ...d, elapsed: Math.round(elapsed * 10) / 10 });
    if (!d.running) {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      loadJobs(); loadStats(); loadFiles();
    } else if (d.found > 0) {
      loadJobs();
    }
  }

  async function analyse() {
    setRerr(""); setRstatus("");
    const f = fileRef.current?.files?.[0];
    if (f && f.size > maxUploadMb * 1024 * 1024) {
      setRerr(`"${f.name}" is ${(f.size / 1048576).toFixed(1)} MB, over the ${maxUploadMb} MB limit. `
             + "Export a smaller PDF, or paste the text below instead.");
      return;
    }
    if (!f && !text.trim()) {
      setRerr("Choose a resume file, or paste your resume text below.");
      return;
    }
    setRstatus("reading…"); setAnalysing(true);
    const fd = new FormData();
    if (f) fd.append("file", f);
    fd.append("text", text);
    fd.append("locations", locations);
    try {
      const d = await api("/api/resume", { method: "POST", body: fd });
      setProfile(d.profile);
      setRstatus("read " + d.source);
      const det = d.profile._detected || {};
      if (det.exp_min != null) setExpMin(det.exp_min);
      if (det.exp_max != null) setExpMax(det.exp_max);
      setFexpMin(det.exp_min != null ? det.exp_min : "");
      setFexpMax(det.exp_max != null ? det.exp_max : "");
      setDetected(d.profile);
    } catch (e) {
      setRerr(e.message); setRstatus("");
    } finally { setAnalysing(false); }
  }

  function toggleSource(name, on) {
    setSources(prev => {
      const next = new Set(prev);
      if (on) next.add(name); else next.delete(name);
      return next;
    });
  }

  async function startRun() {
    if (!profile) {
      setRerr("Analyse a resume first — the search terms come from it.");
      return;
    }
    if (!sources.size) { alert("Pick at least one source."); return; }
    const eMin = expMin === "" ? null : +expMin;
    const eMax = expMax === "" ? null : +expMax;
    if (eMin != null && eMax != null && eMin > eMax) {
      alert("Minimum years cannot be greater than maximum years."); return;
    }
    setProgressOpen(true);
    setRun(r => ({ ...r, log: [] }));
    const body = {
      profile, options: {
        sources: [...sources], target: +target || 100,
        since_days: since ? +since : null,
        remote_only: remoteOnly,
        exp_min: eMin, exp_max: eMax,
        include_unknown_exp: expUnknown,
        export: doExport,
      }
    };
    try {
      await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify(body) });
    } catch (e) {
      setRun(r => ({ ...r, log: [e.message] }));
      return;
    }
    pollStatus();
  }

  async function setJobStatus(jobId, newStatus, prevStatus) {
    setJobs(js => js.map(j => j.job_id === jobId ? { ...j, status: newStatus } : j));
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/status`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      loadStats();
    } catch (e) {
      setJobs(js => js.map(j => j.job_id === jobId ? { ...j, status: prevStatus } : j));
      setJobsErr(e.message);
    }
  }

  function queueApply(job) {
    const p = loadPending().filter(x => x.id !== job.id);
    p.push(job);
    savePending(p);
    setPending(p);
  }

  async function answerPending(job, ans) {
    const rest = loadPending().filter(x => x.id !== job.id);
    savePending(rest); setPending(rest);
    if (ans) {
      try {
        await setJobStatus(job.id, ans, "");
        loadStats();
      } catch (e) { setJobsErr(e.message); }
    }
  }

  async function doExportNow() {
    setDberr(""); setDbok("");
    try {
      const d = await api("/api/export", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ format: "csv" }) });
      setDbok(`Exported ${d.rows} rows.`); loadFiles();
    } catch (e) { setDberr(e.message); }
  }

  async function doPrune() {
    if (!confirm("Delete rows not seen in the last 45 days?")) return;
    setDberr(""); setDbok("");
    try {
      const d = await api("/api/prune", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ days: 45 }) });
      setDbok(`Removed ${d.removed} rows.`); loadStats(); loadJobs();
    } catch (e) { setDberr(e.message); }
  }

  const runStatusText = run.running
    ? <><span className="spin" />{run.found || 0} found
        {run.per_source && Object.keys(run.per_source).length
          ? " — " + Object.entries(run.per_source).map(([k, v]) => `${k} ${v}`).join(" · ") : ""}
        {" · "}{run.elapsed}s</>
    : (run.finished ? `done — ${run.kept} matches in ${run.elapsed}s` : "");

  const job0 = pending[0];

  return (
    <>
      <header>
        <h1>Job Scraper</h1>
        <span className="sub">LinkedIn + Greenhouse + Ashby + Lever + Workable + Workday, scored against your resume</span>
        <a className="btnlink ghostlink" href="/profile" style={{ marginLeft: "auto" }}>Apply kit →</a>
      </header>
      <main>
        <div className="grid">
          <div className="col-left">
            <div className="card">
              <h2>Resume</h2>
              <label>Upload (PDF / DOCX / TXT / MD)</label>
              <input type="file" ref={fileRef} accept=".pdf,.docx,.txt,.md" />
              <label>Or paste text</label>
              <textarea value={text} onChange={e => setText(e.target.value)}
                        placeholder="Paste your resume here if you'd rather not upload a file" />
              <label>Target locations (optional)</label>
              <input type="text" value={locations} onChange={e => setLocations(e.target.value)}
                     placeholder="Bengaluru, India, Dublin, Remote" />
              <div className="row" style={{ marginTop: 12 }}>
                <button id="analyse" disabled={analysing} onClick={analyse}>Analyse resume</button>
                <span className="muted">{rstatus}</span>
              </div>
              <div className="err">{rerr}</div>
              {detected && (
                <div id="detected">
                  <div className="hint" style={{ marginTop: 15 }}>
                    Experience: <b>{detected._detected?.years != null ? detected._detected.years + " years" : "not stated"}</b>
                    {detected._detected?.exp_min != null
                      ? <> · searching <b>{detected._detected.exp_min}–{detected._detected.exp_max} yrs</b></> : ""} ·{" "}
                    Must-have: <b>{(detected.scoring.must_have_terms || []).join(", ")}</b>
                  </div>
                  <div className="chips">
                    {(detected._detected?.skills || []).slice(0, 22).map((s, i) => (
                      <span className="chip" key={i}><b>{s.term}</b> ×{s.count}</span>
                    ))}
                  </div>
                  <div className="hint">Search keywords: {(detected.keywords || []).join(" · ")}</div>
                </div>
              )}
            </div>

            <div className="card">
              <h2>Sources &amp; filters</h2>
              {SOURCE_OPTS.map(([id, name, label, note]) => (
                <label className="chk" key={id}>
                  <input type="checkbox" checked={sources.has(name)}
                         onChange={e => toggleSource(name, e.target.checked)} /> {label}{" "}
                  {note && <span className="muted">{note}</span>}
                </label>
              ))}
              <label className="chk" style={{ marginTop: 14 }}>
                <input type="checkbox" checked={remoteOnly}
                       onChange={e => { setRemoteOnly(e.target.checked); setFremote(e.target.checked); }} />
                {" "}Remote only
              </label>
              <label>Years of experience</label>
              <div className="row">
                <input type="number" value={expMin} onChange={e => setExpMin(e.target.value)}
                       placeholder="min" min="0" max="40" style={{ width: 78 }} />
                <input type="number" value={expMax} onChange={e => setExpMax(e.target.value)}
                       placeholder="max" min="0" max="40" style={{ width: 78 }} />
              </div>
              <label className="chk" style={{ marginTop: 8 }}>
                <input type="checkbox" checked={expUnknown} onChange={e => setExpUnknown(e.target.checked)} />
                {" "}Include jobs that don't state years
              </label>
              <label>Posted within (days)</label>
              <input type="number" value={since} onChange={e => setSince(e.target.value)} min="1" max="365" />
              <label>Target matches</label>
              <input type="number" value={target} onChange={e => setTarget(e.target.value)} min="1" />
              <label className="chk" style={{ marginTop: 12 }}>
                <input type="checkbox" checked={doExport} onChange={e => setDoExport(e.target.checked)} />
                {" "}Write CSV/JSON/Markdown when done
              </label>
              <div className="row" style={{ marginTop: 14 }}>
                <button disabled={run.running || !profile} onClick={startRun}>Fetch jobs</button>
                <span className="muted">{run.running ? runStatusText : ""}</span>
              </div>
            </div>

            <div className="card">
              <h2>Apply kit</h2>
              <div className="muted" style={{ fontSize: 12.5 }}>{applicantSummary}</div>
              <a className="btnlink" href="/profile" style={{ marginTop: 10, display: "inline-block" }}>Edit your details →</a>
            </div>

            <div className="card">
              <h2>Database</h2>
              {stats ? (
                <div className="muted">
                  <div className="stat"><span className="muted">total (incl. rejected)</span><b>{stats.total || 0}</b></div>
                  <div className="stat"><span className="muted">rejected</span><b>{stats.rejected || 0}</b></div>
                  <div className="stat"><span className="muted">avg score</span><b>{stats.avg_score ? stats.avg_score.toFixed(1) : "—"}</b></div>
                  <div className="stat"><span className="muted">best score</span><b>{stats.max_score ? stats.max_score.toFixed(1) : "—"}</b></div>
                  {Object.entries(stats.by_source || {}).map(([k, v]) => (
                    <div className="stat" key={k}><span className="muted">{k}</span><b>{v}</b></div>
                  ))}
                  {Object.entries(stats.by_status || {}).filter(([k]) => k !== "none").map(([k, v]) => (
                    <div className="stat" key={k}><span className="muted">{k}</span><b>{v}</b></div>
                  ))}
                </div>
              ) : <div className="muted">{statsErr || "loading…"}</div>}
              <div className="row" style={{ marginTop: 13 }}>
                <button className="ghost" onClick={doExportNow}>Export now</button>
                <button className="ghost" onClick={doPrune}>Prune &gt;45d</button>
              </div>
              <div className="err">{dberr}</div>
              <div className="ok">{dbok}</div>
              {files.length > 0 && (
                <div>
                  <div className="hint" style={{ marginTop: 13 }}>Exports</div>
                  {files.slice(0, 20).map(f => (
                    <div className="stat" key={f.name}>
                      <a href={`/download/${encodeURIComponent(f.name)}`}>{f.name}</a>
                      <span className="muted">{(f.size / 1024).toFixed(0)} KB</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="col-right">
            <div className="card progress-card">
              {isAdmin() ? (
                <details id="progress" open={progressOpen} onToggle={e => setProgressOpen(e.target.open)}>
                  <summary><h2 style={{ display: "inline", margin: 0 }}>Progress</h2>
                    <span className="muted">{runStatusText}</span></summary>
                  <pre id="log">{run.log && run.log.length ? run.log.join("\n")
                    : "Idle. Analyse a resume, pick your sources, then hit “Fetch jobs”."}</pre>
                </details>
              ) : (
                // Non-admin visitors get the summary only — no expand
                // arrow, no scrape log. Add ?admin=<token> to the URL once
                // to unlock the full log in this browser.
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h2 style={{ margin: 0 }}>Status</h2>
                  <span className="muted">{runStatusText || "Idle"}</span>
                </div>
              )}
            </div>

            <div className="card results-card">
              <h2>Results</h2>
              <div className="row">
                <input type="text" value={q} onChange={e => setQ(e.target.value)}
                       placeholder="filter title / company / location" style={{ flex: 1, minWidth: 170 }} />
                <select value={fsource} onChange={e => setFsource(e.target.value)} style={{ width: "auto" }}>
                  <option value="">all sources</option>
                  <option value="linkedin">LinkedIn</option>
                  <option value="greenhouse">Greenhouse</option>
                  <option value="ashby">Ashby</option>
                  <option value="lever">Lever</option>
                  <option value="workable">Workable</option>
                  <option value="remoteok">Remote OK</option>
                  <option value="workday">Workday</option>
                </select>
                <input type="number" value={fmin} onChange={e => setFmin(e.target.value)}
                       placeholder="min score" style={{ width: 98 }} />
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                <span className="muted" style={{ fontSize: 12 }}>Years of experience</span>
                <input type="number" value={fexpMin} onChange={e => setFexpMin(e.target.value)}
                       placeholder="min" min="0" max="40" style={{ width: 72 }} />
                <span className="muted">to</span>
                <input type="number" value={fexpMax} onChange={e => setFexpMax(e.target.value)}
                       placeholder="max" min="0" max="40" style={{ width: 72 }} />
                <label className="chk" style={{ margin: 0 }}>
                  <input type="checkbox" checked={fexpUnknown} onChange={e => setFexpUnknown(e.target.checked)} />
                  <span className="muted" style={{ fontSize: 12 }}>incl. unstated</span>
                </label>
                <label className="chk" style={{ margin: 0 }}>
                  <input type="checkbox" checked={fremote} onChange={e => setFremote(e.target.checked)} />
                  <span className="muted" style={{ fontSize: 12 }}>remote only</span>
                </label>
                <select value={fstatus} onChange={e => setFstatus(e.target.value)} style={{ width: "auto" }}>
                  <option value="">any status</option>
                  <option value="none">not applied</option>
                  <option value="any">tracked</option>
                  <option value="applied">applied</option>
                  <option value="interview">interview</option>
                  <option value="offer">offer</option>
                  <option value="rejected">rejected</option>
                  <option value="saved">saved</option>
                </select>
                <select value={flimit} onChange={e => setFlimit(e.target.value)} style={{ width: "auto" }}>
                  <option value="300">show 300</option>
                  <option value="1000">show 1000</option>
                  <option value="5000">show all</option>
                </select>
                <button className="ghost" onClick={loadJobs}>Refresh</button>
              </div>
              {job0 && (
                <div className="askbar">
                  <span>Did you apply to <b>{job0.title || job0.id}</b>{job0.company ? " at " + job0.company : ""}?</span>
                  <button className="ghost" onClick={() => answerPending(job0, "applied")}>Yes, applied</button>
                  <button className="ghost" onClick={() => answerPending(job0, "")}>Not yet</button>
                  {pending.length > 1 && <span className="muted">+{pending.length - 1} more</span>}
                </div>
              )}
              <div className="muted" style={{ margin: "11px 0 8px" }}>
                {jobsErr || (
                  (jobsMeta.total > jobsMeta.count
                    ? `showing ${jobsMeta.count} of ${jobsMeta.total} matching jobs`
                    : `${jobsMeta.total} job${jobsMeta.total === 1 ? "" : "s"}`)
                  + (fremote ? "  ·  remote only" : "")
                  + ((fexpMin !== "" || fexpMax !== "")
                    ? `  ·  ${fexpMin || 0}–${fexpMax || "any"} yrs experience` : "")
                )}
              </div>
              <div className="scroll results-scroll">
                <table>
                  <thead><tr>
                    <th className="c-score">Score</th><th className="c-title">Title</th>
                    <th className="c-company">Company</th><th className="c-loc">Location</th>
                    <th className="c-exp">Exp</th><th className="c-posted">Posted</th>
                    <th className="c-source">Src</th><th className="c-link"></th>
                  </tr></thead>
                  <tbody>
                    {jobs.length ? jobs.map(j => (
                      <tr key={j.job_id} className={j.status ? "done" : ""}>
                        <td className="c-score score">{(j.score ?? 0).toFixed(0)}</td>
                        <td className="c-title">{j.title}
                          <div className="rowsub">{[j.company, j.location].filter(Boolean).join(" · ")}</div>
                          <div className="terms">{shortTerms(j.matched_terms)}</div>
                        </td>
                        <td className="c-company">{j.company}</td>
                        <td className="c-loc muted">{j.location}{j.is_remote && <span className="tag"> remote</span>}</td>
                        <td className="c-exp muted">{expLabel(j)}</td>
                        <td className="c-posted muted">{j.posted_date || "—"}</td>
                        <td className="c-source">
                          <span className={`tag ${j.source}`}>
                            <span className="full">{j.source}</span>
                            <span className="abbr">{SOURCE_ABBR[j.source] || j.source.slice(0, 2)}</span>
                          </span>
                        </td>
                        <td className="c-link" data-status={j.status || ""}>
                          <a className="apply" href={`/apply/${encodeURIComponent(j.job_id)}`}
                             target="_blank" rel="noopener noreferrer"
                             onClick={() => queueApply({ id: j.job_id, title: j.title, company: j.company })}>Apply</a>
                          <select className={`statussel${j.status ? " set" : ""}`} value={j.status || ""}
                                  onChange={e => setJobStatus(j.job_id, e.target.value, j.status || "")}>
                            {["", "applied", "interview", "offer", "rejected", "saved"].map(v => (
                              <option value={v} key={v}>{v || "not applied"}</option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    )) : (
                      <tr><td colSpan="8" className="muted" style={{ padding: 22 }}>No jobs match.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </main>
    </>
  );
}
