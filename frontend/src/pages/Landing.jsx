import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, ArrowUpRight, BriefcaseBusiness, Check, CheckCheck, ChevronRight, FileText, Fingerprint, Layers3, Menu, ScanText, ShieldCheck, Sparkles, Target, Upload, X } from "lucide-react";
import "./landing.css";
import NavMascot from "../components/NavMascot";
import BrandLogo from "../components/BrandLogo";

const STEPS = [
  { icon: Upload, title: "Set up your profile once", text: "Upload your resume and save your contact details, skills, and experience in one reusable master profile." },
  { icon: Fingerprint, title: "Paste a job link", text: "Found a role you want? Paste its job posting URL into Autogram and start your application." },
  { icon: ArrowUpRight, title: "Let Autogram fill the form", text: "Autogram opens the job page in a browser and fills the application from your profile, pausing for verification or answers it needs from you." },
];
const FEATURES = [
  { icon: Fingerprint, title: "AI-powered profile intelligence", text: "Give your professional story structure, so your information is ready for the next opportunity." },
  { icon: ScanText, title: "Smart resume understanding", text: "Start from your resume, extract key details, and review them before adding them to your profile." },
  { icon: Layers3, title: "Organized professional information", text: "Keep your skills, experience, education, and documents in a workspace that grows with you." },
  { icon: ArrowUpRight, title: "Browser-powered applications", text: "Paste a job URL. Autogram opens it in a browser and fills application fields using your saved profile." },
  { icon: Target, title: "Career opportunity intelligence", text: "Understand how opportunities align with your resume, skills, and experience." },
  { icon: ShieldCheck, title: "You stay in control", text: "Choose your approval settings, review answers, and step in whenever your input is needed." },
];

function Brand() {
  return <Link to="/" className="inline-flex shrink-0" aria-label="Autogram home"><BrandLogo className="landing-logo" /></Link>;
}

function ProfilePreview({ compact = false }) {
  return <div className={`product-preview ${compact ? "preview-compact" : ""}`} aria-label="Illustrative Autogram profile preview">
    <div className="preview-top"><span className="flex items-center gap-2"><Sparkles size={15} /> Your career workspace</span><span className="preview-caption">Product preview</span></div>
    <div className="preview-body">
      <div className="flex items-center gap-3"><div className="preview-avatar">MA</div><div><p className="text-base font-semibold text-slate-900">Mehul Agarwal</p><p className="mt-1 text-xs text-slate-500">Product designer · Building what’s next</p></div><span className="ml-auto text-brand-600"><CheckCheck size={20} /></span></div>
      <div className="preview-completion"><div className="flex justify-between text-xs"><span className="font-medium">Profile completion</span><span className="font-semibold text-brand-700">85%</span></div><div className="mt-3 h-1.5 rounded-full bg-brand-100"><div className="h-full w-[85%] rounded-full bg-brand-500" /></div><p className="mt-2 text-[10px] text-slate-500">A little more you. A stronger foundation.</p></div>
      <div className="preview-document"><span className="rounded-lg bg-brand-50 p-2 text-brand-600"><FileText size={20} /></span><div className="min-w-0 flex-1"><p className="text-xs font-semibold">Mehul_Agarwal_Resume.pdf</p><p className="mt-1 text-[10px] text-slate-500">Resume analysis · Details organized</p></div><Check size={16} className="text-brand-600" /></div>
      <div className="mt-5"><p className="preview-label">Your strengths, connected</p><div className="mt-3 flex flex-wrap gap-2">{["Product design", "User research", "Figma", "Design systems"].map(skill => <span className="preview-skill" key={skill}>{skill}</span>)}</div></div>
      {!compact && <div className="mt-5 border-t border-slate-100 pt-4"><p className="preview-label">Experience</p><div className="mt-3 flex items-center gap-3"><BriefcaseBusiness size={18} className="text-slate-400" /><div><p className="text-xs font-semibold">Senior Product Designer</p><p className="mt-1 text-[10px] text-slate-500">Studio North · 2022–present</p></div></div></div>}
    </div>
  </div>;
}

export default function Landing({ user }) {
  const landingRef = useRef(null);
  useEffect(() => {
    if (!window.IntersectionObserver || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const elements = landingRef.current.querySelectorAll(".section-intro, .step-card, .feature-card, .preview-section-heading, .dashboard-preview, .about-section, .final-cta-inner");
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("landing-revealed");
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.08 });
    elements.forEach(element => observer.observe(element));
    return () => observer.disconnect();
  }, []);
  const [menuOpen, setMenuOpen] = useState(false);
  const destination = user ? "/dashboard" : "/signup";
  const cta = user ? "Open dashboard" : "Get Started";
  const navigation = <><a href="#how-it-works" onClick={() => setMenuOpen(false)}>How it works</a><a href="#features" onClick={() => setMenuOpen(false)}>Features</a><a href="#about" onClick={() => setMenuOpen(false)}>About</a></>;
  return <div ref={landingRef} className="landing">
    <a href="#landing-main" className="skip-link">Skip to content</a>
    <header className="landing-header">
      <div className="landing-nav"><Brand /><nav className="landing-links hidden md:flex" aria-label="Main navigation">{navigation}</nav><div className="flex items-center gap-3 sm:gap-5"><Link className="landing-signin hidden sm:block" to={user ? "/dashboard" : "/login"}>{user ? "Dashboard" : "Sign In"}</Link><Link to={destination} className="landing-button nav-cta">{cta}<ArrowUpRight size={15} /></Link><button className="btn-icon md:hidden" aria-label={menuOpen ? "Close navigation" : "Open navigation"} aria-expanded={menuOpen} aria-controls="landing-mobile-nav" onClick={() => setMenuOpen(!menuOpen)}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</button></div></div>
      {menuOpen && <nav id="landing-mobile-nav" className="landing-mobile-nav" aria-label="Mobile navigation" onKeyDown={event => { if (event.key === "Escape") setMenuOpen(false); }}>{navigation}<Link to={user ? "/dashboard" : "/login"}>{user ? "Dashboard" : "Sign In"}</Link></nav>}
      <NavMascot />
    </header>

    <main id="landing-main">
      <section className="landing-hero landing-container">
        <div className="hero-copy"><p className="landing-eyebrow"><span className="h-1.5 w-1.5 rounded-full bg-brand-500" /> YOUR JOB LINK. YOUR PROFILE. LESS FORM FILLING.</p><h1>Paste a job link. <span>Autogram fills the application.</span></h1><p className="hero-description">Set up your resume and profile once. Then paste a job link: Autogram automatically opens the job page in a browser and fills the application using your saved details. It pauses when it needs your input and follows your submission settings.</p><div className="mt-8 flex flex-wrap items-center gap-6"><Link to={destination} className="landing-button">{cta}<ArrowRight size={17} /></Link><Link to={user ? "/dashboard" : "/login"} className="landing-text-link">{user ? "Go to your workspace" : "Sign In"}<ChevronRight size={15} /></Link></div><p className="hero-reassurance"><ShieldCheck size={15} /> You handle verification. You choose when to submit.</p></div>
        <div className="hero-visual"><div className="hero-orbit" aria-hidden="true" /><div className="hero-preview"><ProfilePreview compact /></div><div className="hero-insight"><span className="insight-icon"><Sparkles size={19} /></span><div><p className="text-xs font-semibold">Your experience has potential.</p><p className="mt-1 text-[11px] text-slate-500">Give it a clearer path forward.</p></div><ArrowUpRight size={18} className="ml-auto text-brand-600" /></div><span className="hero-visual-note">A clearer picture of what makes you, you.</span></div>
      </section>

      <div className="landing-principles landing-container"><p>BUILT AROUND YOUR NEXT MOVE</p><span><Fingerprint size={18} /> One professional identity</span><span><Sparkles size={18} /> AI-powered assistance</span><span><ShieldCheck size={18} /> Human control, always</span></div>

      <section id="how-it-works" className="landing-section landing-container"><div className="section-intro"><div><p className="landing-eyebrow">A SIMPLER WAY FORWARD</p><h2>From job link.<br />To a filled application.</h2></div><p>No more starting every application from scratch. Set up once, bring a job link, and let Autogram do the repetitive typing.</p></div><div className="steps-grid">{STEPS.map(({ icon: Icon, title, text }, index) => <article className="step-card" key={title}><div className="step-top"><span className="step-icon"><Icon size={22} /></span><span className="step-number">0{index + 1}</span></div><h3>{title}</h3><p>{text}</p>{index < 2 && <ArrowRight className="step-connector" size={19} aria-hidden="true" />}</article>)}</div></section>

      <section id="features" className="features-section"><div className="landing-container"><div className="section-intro"><div><p className="landing-eyebrow">WHY AUTOGRAM</p><h2>One profile.<br /><span className="text-brand-600">Smarter applications.</span></h2></div><p>Instead of filling out the same information for every opportunity, build your professional identity once and use it intelligently.</p></div><div className="features-grid">{FEATURES.map(({ icon: Icon, title, text }) => <article className="feature-card" key={title}><Icon size={23} strokeWidth={1.5} /><h3>{title}</h3><p>{text}</p></article>)}</div></div></section>

      <section className="landing-section landing-container"><div className="preview-section-heading"><p className="landing-eyebrow">MEET YOUR CAREER WORKSPACE</p><h2>A little less scattered.<br />A lot more possibility.</h2><p>Your professional story, opportunities, and next steps.<br className="hidden sm:block" /> Finally, in the same place.</p></div><div className="dashboard-preview"><div className="dashboard-preview-bar"><span className="flex items-center gap-2 font-semibold"><Sparkles size={17} /> autogram</span><span className="preview-caption">Illustrative workspace · Sample data</span></div><div className="dashboard-preview-content"><ProfilePreview /><div className="preview-opportunities"><div className="flex items-center justify-between"><div><p className="preview-label">YOUR NEXT CHAPTER</p><h3 className="mt-2 text-lg font-semibold">Room to grow.</h3></div><Target size={22} className="text-brand-600" /></div><p className="mt-2 text-xs leading-relaxed text-slate-500">Understand opportunities through the lens of your experience.</p><div className="opportunity-card"><div className="flex items-center gap-3"><span className="opportunity-logo">N</span><div><p className="text-sm font-semibold">Senior Product Designer</p><p className="mt-1 text-xs text-slate-500">Northstar · Remote</p></div></div><div className="mt-4 flex flex-wrap gap-2"><span className="preview-skill">Design systems</span><span className="preview-skill">Product strategy</span></div><div className="mt-5 flex items-center justify-between border-t border-slate-100 pt-3 text-xs"><span className="text-slate-500">Experience alignment</span><span className="font-semibold text-brand-700">Strong match</span></div></div><div className="application-progress"><div className="flex items-center justify-between"><p className="text-sm font-semibold">Application progress</p><span className="badge badge-brand">For your review</span></div><div className="mt-5 flex items-center gap-2 text-[10px] font-medium"><span className="flex items-center gap-1 text-brand-700"><Check size={13} /> Profile ready</span><span className="h-px flex-1 bg-brand-200" /><span className="flex items-center gap-1 text-brand-700"><Check size={13} /> Details filled</span><span className="h-px flex-1 bg-brand-200" /><span className="text-brand-700">Your review</span></div></div></div></div></div></section>

      <section id="about" className="about-section landing-container"><p className="landing-eyebrow">THE IDEA BEHIND AUTOGRAM</p><h2>More momentum.<br />Less starting over.</h2><div><p>Your career is more than a collection of forms. Autogram is an AI-powered career platform designed to help you organize what you bring, understand where you could go, and take the next step with intention.</p><p className="mt-4 font-medium text-brand-700">Technology for the process. You at the center.</p></div></section>

      <section className="final-cta landing-container"><div className="final-cta-inner"><span className="final-cta-icon"><Sparkles size={26} /></span><p className="landing-eyebrow">YOUR NEXT MOVE STARTS HERE</p><h2>Ready to make your<br />job search smarter?</h2><p>Save your profile once. Bring your next job link. Let Autogram open the browser and handle the form filling.</p><Link to={destination} className="landing-button">{user ? "Open your dashboard" : "Get Started with Autogram"}<ArrowRight size={17} /></Link></div></section>
    </main>
    <footer className="landing-footer landing-container"><Brand /><p>Built for your next chapter.</p><span>© {new Date().getFullYear()} Autogram</span><a href="#how-it-works">How it works <ArrowUpRight size={13} /></a></footer>
  </div>;
}
