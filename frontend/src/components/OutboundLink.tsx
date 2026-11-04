// A link out of the app (spec §16.6): it opens in a new tab with `rel="noopener noreferrer nofollow"`, and says
// so to assistive technology. Only an absolute `http:` or `https:` URL becomes a link; anything else (a
// `javascript:` or `data:` URL, a relative path, what does not parse) renders the children as plain text, so
// the component is safe whatever the server sends. The text is the caller's.
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export const OUTBOUND_REL = "noopener noreferrer nofollow";

export interface OutboundLinkProps {
  href: string;
  children: ReactNode;
  className?: string;
}

const WEB_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:"]);

/** Whether `href` is an absolute http(s) URL. */
function isWebUrl(href: string): boolean {
  try {
    return WEB_PROTOCOLS.has(new URL(href).protocol);
  } catch {
    return false; // no absolute URL
  }
}

export function OutboundLink({ href, children, className }: OutboundLinkProps) {
  const { t } = useTranslation();
  if (!isWebUrl(href)) {
    return <span className={className}>{children}</span>;
  }
  return (
    <a href={href} target="_blank" rel={OUTBOUND_REL} className={className}>
      {children}{" "}
      <span className="visually-hidden">{t("link.new_tab")}</span>
    </a>
  );
}
