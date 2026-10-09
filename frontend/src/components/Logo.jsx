import { Link } from "react-router-dom";

export const Logo = ({ size = 44, withWord = true, className = "", testid = "header-logo-link" }) => (
  <Link to="/" className={`inline-flex items-center gap-3 ${className}`} aria-label="ANORA home" data-testid={testid}>
    <img src="/anora-logo-512.png" alt="ANORA — Fashion for Her" width={size} height={size} style={{ width: size, height: size }} className="shrink-0 object-contain" />
    {withWord && <span className="hidden font-serif text-2xl tracking-[0.42em] sm:inline">ANORA</span>}
  </Link>
);
