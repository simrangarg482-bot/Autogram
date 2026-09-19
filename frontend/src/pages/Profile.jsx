import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Loader2, Save, Plus, Trash2, GraduationCap, Briefcase, Sparkles, UserCircle2, CloudUpload } from "lucide-react";
import { api } from "../api";
import ApplicationProfileSection from "../components/ApplicationProfileSection";

const FIELD_GROUPS = [
  {
    title: "Identity", fields: [
      ["full_name", "Full name"], ["preferred_name", "Preferred name"], ["email", "Email"], ["phone", "Phone"],
      ["linkedin_url", "LinkedIn"], ["github_url", "GitHub"], ["portfolio_url", "Portfolio"], ["website_url", "Website"],
    ],
  },
  {
    title: "Location", fields: [
      ["location", "Location (freeform)"], ["city", "City"], ["state", "State"], ["country", "Country"],
      ["postal_code", "Postal code"], ["time_zone", "Time zone"],
    ],
  },
  {
    title: "Professional", fields: [
      ["current_company", "Current company"], ["current_role", "Current role"],
      ["years_of_experience", "Years of experience", "number"], ["notice_period_days", "Notice period (days)", "number"],
      ["expected_salary", "Expected salary", "number"], ["expected_salary_currency", "Currency"],
      ["highest_education_level", "Highest education level"], ["referral_source", "How did you hear about roles?"],
    ],
  },
  {
    title: "Work Authorization & Compliance", fields: [
      ["work_authorization", "Work authorization (freeform)"], ["visa_status", "Visa status (freeform)"],
      ["visa_type", "Visa type"], ["remote_preference", "Remote preference"],
    ],
  },
];

const BOOL_FIELDS = [
  ["work_authorized", "Authorized to work now"], ["requires_sponsorship", "Will require sponsorship"],
  ["willing_to_relocate", "Willing to relocate"], ["willing_to_travel", "Willing to travel"],
  ["willing_background_check", "Willing to complete a background check"],
];

const SKILL_KEYS = ["programming_languages", "frameworks", "tools", "certifications", "technical_skills", "soft_skills"];

function SectionTitle({ icon: Icon, children }) {
  return (
    <h2 className="mb-4 flex items-center gap-2.5 font-semibold text-slate-900">
      <span className="flex h-7 w-7 items-center justify-center rounded-md bg-brand-50 text-brand-600">
        <Icon size={15} />
      </span>
      {children}
    </h2>
  );
}

// Small per-section save action — every section writes through the same
// PATCH/POST /profile call regardless of which button triggered it (the
// profile is saved as one object), so this is one shared button rather than
// N independent save paths.
function SectionSaveButton({ saving, onClick, label = "Save section" }) {
  return (
    <button className="btn-ghost mt-4 text-xs" disabled={saving} onClick={onClick}>
      {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} {label}
    </button>
  );
}

export default function Profile({ toast }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [reviewingDraft, setReviewingDraft] = useState(Boolean(location.state?.resumeDraft));
  const [profile, setProfile] = useState(null);
  const [exists, setExists] = useState(true);
  const [education, setEducation] = useState([]);
  const [experience, setExperience] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newEdu, setNewEdu] = useState({ degree: "", university: "", field_of_study: "", start_date: "", end_date: "" });
  const [newExp, setNewExp] = useState({ company_name: "", job_title: "", start_date: "", end_date: "", description: "" });
  const [skillsText, setSkillsText] = useState({});
  // Résumé-parsed education/experience the user hasn't reviewed yet — shown
  // as one-click suggestions alongside the manual add form. Nothing here is
  // saved until the user clicks "Add" on a specific entry.
  const [suggestedEducation, setSuggestedEducation] = useState(location.state?.resumeDraft?.education || []);
  const [suggestedExperience, setSuggestedExperience] = useState(location.state?.resumeDraft?.experience || []);
  // Onboarding entry point (see App.jsx::handleAuthed): a brand-new signup
  // lands here with nothing to review yet, so this prompt uploads AND
  // auto-saves the résumé in one step — unlike the résumé-suggestion cards
  // above, which only ever stage suggestions the user must accept.
  const [promptUpload, setPromptUpload] = useState(Boolean(location.state?.promptResumeUpload));
  const [uploadingResume, setUploadingResume] = useState(false);
  const resumeInputRef = useRef(null);

  function mergeSkillsText(savedSkills, draftSkills) {
    return Object.fromEntries(SKILL_KEYS.map((k) => {
      const saved = (savedSkills?.[k] || []).join(", ");
      if (saved) return [k, saved];
      const draftValues = draftSkills?.[k];
      return [k, draftValues?.length ? draftValues.join(", ") : ""];
    }));
  }

  async function load() {
    const draft = location.state?.resumeDraft || {};
    try {
      const p = await api.getProfile();
      setProfile({ ...p, ...Object.fromEntries(Object.entries(draft).filter(([key]) => key !== "skills" && key !== "education" && key !== "experience" && !p[key])) });
      setSkillsText(mergeSkillsText(p.skills, draft.skills));
      const [edu, exp] = await Promise.all([api.listEducation(), api.listExperience()]);
      setEducation(edu);
      setExperience(exp);
    } catch (error) {
      if (error.status === 404) {
        setExists(false);
        // No profile yet — the draft (if any) IS the starting point, not just
        // a gap-filler over an existing one (that merge only makes sense in
        // the success path above, where `p` already has real saved facts).
        const { skills: _skills, education: _education, experience: _experience, ...scalarDraft } = draft;
        setProfile({ ...scalarDraft });
        setSkillsText(mergeSkillsText(null, draft.skills));
      } else {
        toast(error.message, "error");
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function setField(key, value) {
    setProfile((p) => ({ ...p, [key]: value }));
  }

  async function save() {
    setSaving(true);
    try {
      if (exists) {
        const updated = await api.updateProfile(profile);
        setProfile(updated);
      } else {
        const created = await api.createProfile(profile);
        setProfile(created);
        setExists(true);
      }
      toast("Profile saved.", "success");
      setReviewingDraft(false);
      navigate("/profile", { replace: true, state: null });
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function onUploadResume(file) {
    if (!file) return;
    setUploadingResume(true);
    try {
      const document = await api.uploadDocument(file, { documentType: "resume" });
      const updated = await api.applyResumeDraft(document.document_id);
      const [edu, exp] = await Promise.all([api.listEducation(), api.listExperience()]);
      setProfile(updated);
      setExists(true);
      setEducation(edu);
      setExperience(exp);
      setSkillsText(mergeSkillsText(updated.skills, null));
      toast("Profile filled in from your resume — review and edit below.", "success");
      setPromptUpload(false);
      navigate("/profile", { replace: true, state: null });
    } catch (e) {
      // Keep the prompt up so the user can retry with a different file —
      // extraction failing is expected for some resumes (rule: never invent
      // data, so a low-signal file is a hard failure, not a partial guess).
      toast(e.message, "error");
    } finally {
      setUploadingResume(false);
    }
  }

  async function saveSkills() {
    setSaving(true);
    try {
      const body = Object.fromEntries(
        SKILL_KEYS.map((k) => [k, skillsText[k].split(",").map((s) => s.trim()).filter(Boolean)])
      );
      const updated = await api.setSkills(body);
      setProfile(updated);
      toast("Skills saved.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function addEducation() {
    if (!newEdu.degree && !newEdu.university) return toast("Add at least a degree or university.", "error");
    try {
      const entry = await api.addEducation(newEdu);
      setEducation((e) => [entry, ...e]);
      setNewEdu({ degree: "", university: "", field_of_study: "", start_date: "", end_date: "" });
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function acceptSuggestedEducation(index) {
    const suggestion = suggestedEducation[index];
    try {
      const entry = await api.addEducation(suggestion);
      setEducation((e) => [entry, ...e]);
      setSuggestedEducation((s) => s.filter((_, i) => i !== index));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function addExperience() {
    if (!newExp.company_name && !newExp.job_title) return toast("Add at least a company or job title.", "error");
    try {
      const entry = await api.addExperience(newExp);
      setExperience((e) => [entry, ...e]);
      setNewExp({ company_name: "", job_title: "", start_date: "", end_date: "", description: "" });
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function acceptSuggestedExperience(index) {
    const suggestion = suggestedExperience[index];
    try {
      const entry = await api.addExperience(suggestion);
      setExperience((e) => [entry, ...e]);
      setSuggestedExperience((s) => s.filter((_, i) => i !== index));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  if (loading) return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;
  if (!profile) return <button className="btn-primary" onClick={() => { setLoading(true); load(); }}>Retry loading profile</button>;

  return (
    <div className="animate-fade-up space-y-5">
      {promptUpload && (
        <div className="card border-brand-200 p-5" role="status">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <p className="font-semibold text-slate-900">Upload your resume to get started</p>
              <p className="mt-0.5 text-xs text-slate-500">
                We'll fill in your contact info, experience, education, and skills automatically —
                review and edit anything below afterward. Nothing is guessed.
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <button className="btn-primary" disabled={uploadingResume} onClick={() => resumeInputRef.current?.click()}>
                {uploadingResume ? <Loader2 size={16} className="animate-spin" /> : <CloudUpload size={16} />}
                {uploadingResume ? "Reading your resume…" : "Upload resume"}
              </button>
              <button className="btn-ghost text-xs" disabled={uploadingResume} onClick={() => setPromptUpload(false)}>
                Skip for now
              </button>
            </div>
            <input
              ref={resumeInputRef}
              type="file"
              accept=".pdf,.docx"
              className="hidden"
              onChange={(e) => { onUploadResume(e.target.files[0]); e.target.value = ""; }}
            />
          </div>
        </div>
      )}
      {reviewingDraft && <div className="card border-brand-200 p-4 text-sm" role="status">Review the details extracted from your resume in each section below, then save that section (or use "Save all profile fields" at the bottom). Your existing details have been kept.</div>}
      <div>
        <p className="page-kicker">Your foundation</p>
        <h1 className="page-title">Your master profile</h1>
        <p className="page-subtitle">
          The source of truth automation fills applications from. Nothing here is ever guessed — only what you enter or upload.
        </p>
      </div>

      <nav className="section-nav" aria-label="Profile sections">
        {FIELD_GROUPS.map((group, index) => <a key={group.title} href={`#profile-section-${index}`}>{group.title}</a>)}
        <a href="#profile-screening">Screening</a><a href="#profile-application">Application profile</a><a href="#profile-education">Education</a><a href="#profile-experience">Experience</a><a href="#profile-skills">Skills</a>
      </nav>
      {FIELD_GROUPS.map((group, index) => (
        <div id={`profile-section-${index}`} key={group.title} className="card p-6">
          <SectionTitle icon={UserCircle2}>{group.title}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.fields.map(([key, label, type]) => (
              <label key={key} className="block">
                <span className="field-label">{label}</span>
                <input className="input" type={type || "text"} value={profile[key] ?? ""}
                  onChange={(e) => setField(key, type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value)} />
              </label>
            ))}
          </div>
          {group.title === "Professional" && (
            <label className="mt-3 block">
              <span className="field-label">Professional summary</span>
              <textarea className="input min-h-[88px]" value={profile.professional_summary ?? ""}
                onChange={(e) => setField("professional_summary", e.target.value)} />
            </label>
          )}
          <SectionSaveButton saving={saving} onClick={save} />
        </div>
      ))}

      <div id="profile-screening" className="card p-6">
        <h2 className="mb-1 font-semibold text-slate-900">Yes/No Screening Facts</h2>
        <p className="mb-3 text-xs text-slate-500">Left blank ("never asked") until you set it — automation never guesses these.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {BOOL_FIELDS.map(([key, label]) => (
            <label key={key} className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
              <span className="text-xs text-slate-700">{label}</span>
              <select className="input !w-28 !py-1 text-xs" value={profile[key] === null || profile[key] === undefined ? "" : String(profile[key])}
                onChange={(e) => setField(key, e.target.value === "" ? null : e.target.value === "true")}>
                <option value="">Never asked</option>
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            </label>
          ))}
        </div>
        <SectionSaveButton saving={saving} onClick={save} />
      </div>

      {/* Voluntary EEO answers — its own resource (candidate_demographics), saved independently of the profile fields above. */}
      <ApplicationProfileSection toast={toast} profileExists={exists} />

      {/* Education */}
      <div id="profile-education" className="card p-6">
        <SectionTitle icon={GraduationCap}>Education</SectionTitle>
        <div className="mb-4 grid gap-2 sm:grid-cols-5">
          <input className="input" placeholder="Degree" value={newEdu.degree} onChange={(e) => setNewEdu({ ...newEdu, degree: e.target.value })} />
          <input className="input" placeholder="University" value={newEdu.university} onChange={(e) => setNewEdu({ ...newEdu, university: e.target.value })} />
          <input className="input" placeholder="Field of study" value={newEdu.field_of_study} onChange={(e) => setNewEdu({ ...newEdu, field_of_study: e.target.value })} />
          <input className="input" placeholder="Start (e.g. 2018)" value={newEdu.start_date} onChange={(e) => setNewEdu({ ...newEdu, start_date: e.target.value })} />
          <div className="flex gap-2">
            <input className="input" placeholder="End (e.g. 2022)" value={newEdu.end_date} onChange={(e) => setNewEdu({ ...newEdu, end_date: e.target.value })} />
            <button className="btn-primary !px-3" onClick={addEducation}><Plus size={16} /></button>
          </div>
        </div>
        {suggestedEducation.length > 0 && (
          <div className="mb-4 space-y-2 rounded-lg border border-brand-200 bg-brand-50/40 p-3">
            <p className="text-xs font-medium text-brand-700">Found in your résumé — add the ones that are right:</p>
            {suggestedEducation.map((s, i) => (
              <div key={i} className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm">
                <span className="text-slate-700">{s.degree}{s.field_of_study ? `, ${s.field_of_study}` : ""} — {s.university}{s.end_date ? ` (${s.end_date})` : ""}</span>
                <div className="flex shrink-0 gap-1.5">
                  <button className="btn-ghost !px-2.5 !py-1 text-xs" onClick={() => acceptSuggestedEducation(i)}><Plus size={13} /> Add</button>
                  <button className="text-slate-400 hover:text-red-600" onClick={() => setSuggestedEducation((s) => s.filter((_, x) => x !== i))} title="Dismiss">
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="space-y-2">
          {education.map((e) => (
            <div key={e.education_id} className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm">
              <span className="text-slate-700">{e.degree}{e.field_of_study ? `, ${e.field_of_study}` : ""} — {e.university} ({e.start_date}–{e.end_date || "present"})</span>
              <button className="text-slate-400 hover:text-red-600" onClick={async () => { await api.deleteEducation(e.education_id); setEducation((xs) => xs.filter((x) => x.education_id !== e.education_id)); }}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          {education.length === 0 && <p className="text-xs text-slate-400">No education entries yet.</p>}
        </div>
      </div>

      {/* Experience */}
      <div id="profile-experience" className="card p-6">
        <SectionTitle icon={Briefcase}>Work Experience</SectionTitle>
        <div className="mb-4 grid gap-2 sm:grid-cols-5">
          <input className="input" placeholder="Company" value={newExp.company_name} onChange={(e) => setNewExp({ ...newExp, company_name: e.target.value })} />
          <input className="input" placeholder="Job title" value={newExp.job_title} onChange={(e) => setNewExp({ ...newExp, job_title: e.target.value })} />
          <input className="input" placeholder="Start (e.g. 2021-03)" value={newExp.start_date} onChange={(e) => setNewExp({ ...newExp, start_date: e.target.value })} />
          <input className="input" placeholder="End (blank = current)" value={newExp.end_date} onChange={(e) => setNewExp({ ...newExp, end_date: e.target.value })} />
          <button className="btn-primary" onClick={addExperience}><Plus size={16} /> Add</button>
        </div>
        {suggestedExperience.length > 0 && (
          <div className="mb-4 space-y-2 rounded-lg border border-brand-200 bg-brand-50/40 p-3">
            <p className="text-xs font-medium text-brand-700">Found in your résumé — add the ones that are right:</p>
            {suggestedExperience.map((s, i) => (
              <div key={i} className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm">
                <span className="text-slate-700">{s.job_title} at {s.company_name} ({s.start_date}–{s.end_date || "present"})</span>
                <div className="flex shrink-0 gap-1.5">
                  <button className="btn-ghost !px-2.5 !py-1 text-xs" onClick={() => acceptSuggestedExperience(i)}><Plus size={13} /> Add</button>
                  <button className="text-slate-400 hover:text-red-600" onClick={() => setSuggestedExperience((s) => s.filter((_, x) => x !== i))} title="Dismiss">
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="space-y-2">
          {experience.map((e) => (
            <div key={e.experience_id} className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm">
              <span className="text-slate-700">{e.job_title} at {e.company_name} ({e.start_date}–{e.end_date || "present"})</span>
              <button className="text-slate-400 hover:text-red-600" onClick={async () => { await api.deleteExperience(e.experience_id); setExperience((xs) => xs.filter((x) => x.experience_id !== e.experience_id)); }}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          {experience.length === 0 && <p className="text-xs text-slate-400">No experience entries yet.</p>}
        </div>
      </div>

      {/* Skills */}
      <div id="profile-skills" className="card p-6">
        <SectionTitle icon={Sparkles}>Skills</SectionTitle>
        <div className="grid gap-3 sm:grid-cols-2">
          {SKILL_KEYS.map((k) => (
            <label key={k} className="block">
              <span className="field-label capitalize">{k.replaceAll("_", " ")} (comma-separated)</span>
              <input className="input" value={skillsText[k] || ""} onChange={(e) => setSkillsText({ ...skillsText, [k]: e.target.value })} />
            </label>
          ))}
        </div>
        <button className="btn-primary mt-4" disabled={saving} onClick={saveSkills}>
          {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} Save Skills
        </button>
      </div>

      <div className="card flex items-center justify-between gap-4 p-6">
        <div>
          <p className="font-semibold text-slate-900">Save everything above</p>
          <p className="text-xs text-slate-500">Identity, location, professional, work authorization, and screening fields all save together.</p>
        </div>
        <button className="btn-primary" disabled={saving} onClick={save}>
          {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} Save all profile fields
        </button>
      </div>
    </div>
  );
}
