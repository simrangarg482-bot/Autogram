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
            <path d="M14 23 Q9 23 10 33 L15 34" fill="#d5dfac" stroke="#899b66" strokeWidth="1" />
            <g className="mascot-leg mascot-leg-back"><path d="M19 33 L16 42" stroke="#294e43" strokeWidth="5" strokeLinecap="round" /><path d="M16 43 L20 43" stroke="#213730" strokeWidth="4" strokeLinecap="round" /></g>
            <g className="mascot-arm mascot-arm-back"><path d="M16 24 L11 32" stroke="#c88859" strokeWidth="4" strokeLinecap="round" /></g>
            <path d="M15 22 Q20 19 25 23 L26 34 Q20 37 14 33 Z" fill="#238e76" />
            <path d="M16 23 L14 30" stroke="#d5dfac" strokeWidth="2" strokeLinecap="round" />
            <g className="mascot-leg mascot-leg-front"><path d="M23 34 L26 42" stroke="#376254" strokeWidth="5" strokeLinecap="round" /><path d="M26 43 L30 43" stroke="#213730" strokeWidth="4" strokeLinecap="round" /></g>
            <g className="mascot-arm mascot-arm-front"><path d="M24 25 L28 32" stroke="#e5aa77" strokeWidth="4" strokeLinecap="round" /></g>
            <rect x="18" y="18" width="6" height="5" rx="2" fill="#d99b68" />
            <path d="M12 12 Q12 3 21 3 Q30 4 29 14 L31 16 L28 18 Q25 23 18 20 Q12 18 12 12" fill="#e5aa77" />
            <path d="M12 15 Q7 7 15 3 Q22 -1 28 5 L29 9 Q22 10 20 6 Q19 12 14 12 L14 16 Z" fill="#34382e" />
            <circle cx="26" cy="13" r="1.1" fill="#293d32" />
            <path d="M25 18 Q27 19 28 17" fill="none" stroke="#985b41" strokeWidth="1" strokeLinecap="round" />
            <circle cx="14" cy="15" r="2" fill="#d99b68" />
          </g>
        </svg>
      </div>
    </div>
    <button type="button" className="nav-mascot-toggle" onClick={() => setPaused(value => !value)} aria-label={paused ? "Play mascot animation" : "Pause mascot animation"} title={paused ? "Play mascot animation" : "Pause mascot animation"}>
      {paused ? <Play size={12} /> : <Pause size={12} />}
    </button>
  </div>;
}
