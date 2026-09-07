import { useState } from "react";
import { Pause, Play } from "lucide-react";

export default function NavMascot() {
  const [paused, setPaused] = useState(false);
  return <div className={`nav-mascot-lane ${paused ? "mascot-paused" : ""}`}>
    <div className="nav-mascot-track" aria-hidden="true">
      <div className="nav-mascot-traveler">
        <svg className="nav-mascot-child" viewBox="0 0 40 48" width="32" height="38" focusable="false">
          <ellipse cx="20" cy="46" rx="11" ry="1.5" fill="#173b30" opacity=".12" />
          <g className="mascot-body">
            {/* A student applicant: backpack, interview shirt, and resume folder. */}
            <path d="M14 20 Q7 19 8 26 L8 33 Q10 36 15 34 L17 23 Z" fill="#344e65" stroke="#253d50" strokeWidth="1" />
            <path d="M9 28 L13 28 L13 33 L9 33 Z" fill="#57728a" />
            <path d="M10 21 Q9 17 13 18 L15 20" fill="none" stroke="#253d50" strokeWidth="1.5" />
            <g className="mascot-leg mascot-leg-back"><path d="M19 33 L16 42" stroke="#294e43" strokeWidth="5" strokeLinecap="round" /><path d="M16 43 L20 43" stroke="#213730" strokeWidth="4" strokeLinecap="round" /></g>
            <g className="mascot-arm mascot-arm-back"><path d="M16 24 L11 32" stroke="#c88859" strokeWidth="4" strokeLinecap="round" /></g>
            <path d="M15 21 Q20 19 25 22 L26 34 Q20 36 14 33 Z" fill="#e7f1ec" stroke="#87a999" strokeWidth=".6" />
            <path d="M18 21 L21 24 L19 26 L16 22 M23 21 L21 24 L24 26 L26 23" fill="#fff" stroke="#a4bcaf" strokeWidth=".5" />
            <path d="M21 24 L23 26 L22 32 L20 30 L20 26 Z" fill="#16735f" />
            <path d="M16 22 L14 31" stroke="#344e65" strokeWidth="2" strokeLinecap="round" />
            <g className="mascot-leg mascot-leg-front"><path d="M23 34 L26 42" stroke="#376254" strokeWidth="5" strokeLinecap="round" /><path d="M26 43 L30 43" stroke="#213730" strokeWidth="4" strokeLinecap="round" /></g>
            <g className="mascot-arm mascot-arm-front">
              <path d="M24 24 L26 28" stroke="#e7f1ec" strokeWidth="5" strokeLinecap="round" />
              <path d="M26 28 L29 32" stroke="#e5aa77" strokeWidth="3.5" strokeLinecap="round" />
              <rect x="26" y="29" width="10" height="13" rx="1" fill="#d7b777" stroke="#ad8c4f" strokeWidth=".7" />
              <path d="M28 28 L33 28 L35 30 L35 39 L28 39 Z" fill="#fffef9" />
              <path d="M33 28 L33 30 L35 30" fill="#e5e9df" />
              <circle cx="31" cy="32" r="1.1" fill="#238e76" />
              <path d="M29.5 34 L33.5 34 M29.5 36 L33.5 36 M29.5 38 L32 38" stroke="#87998b" strokeWidth=".65" />
              <circle cx="28" cy="32" r="1.7" fill="#e5aa77" />
            </g>
            <rect x="18" y="18" width="6" height="5" rx="2" fill="#d99b68" />
            <g transform="translate(3 4) scale(.85)">
            <path d="M12 12 Q12 3 21 3 Q30 4 29 14 L31 16 L28 18 Q25 23 18 20 Q12 18 12 12" fill="#e5aa77" />
            <path d="M12 15 Q7 7 15 3 Q22 -1 28 5 L29 9 Q22 10 20 6 Q19 12 14 12 L14 16 Z" fill="#34382e" />
            <circle cx="26" cy="13" r="1.1" fill="#293d32" />
            <path d="M25 18 Q27 19 28 17" fill="none" stroke="#985b41" strokeWidth="1" strokeLinecap="round" />
            <circle cx="14" cy="15" r="2" fill="#d99b68" />
            <path d="M23 11.5 L29 11.5 L29 15 L23 15 Z M23 12 L16 11" fill="none" stroke="#344e45" strokeWidth=".9" strokeLinejoin="round" />
            </g>
          </g>
        </svg>
      </div>
    </div>
    <button type="button" className="nav-mascot-toggle" onClick={() => setPaused(value => !value)} aria-label={paused ? "Play mascot animation" : "Pause mascot animation"} title={paused ? "Play mascot animation" : "Pause mascot animation"}>
      {paused ? <Play size={12} /> : <Pause size={12} />}
    </button>
  </div>;
}
