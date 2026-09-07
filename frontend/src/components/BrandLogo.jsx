/** A reusable vector identity: a forward-moving A and a quiet wordmark. */
export default function BrandLogo({ className = "" }) {
  return <span className={`autogram-logo ${className}`} role="img" aria-label="Autogram">
    <img src="/autogram-mark.svg" alt="" width="44" height="44" aria-hidden="true" />
    <span className="autogram-wordmark" aria-hidden="true">autogram<span className="autogram-wordmark-dot">.</span></span>
  </span>;
}
