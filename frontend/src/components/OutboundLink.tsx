// A link out of the app (spec §16.6): it opens in a new tab with `rel="noopener noreferrer nofollow"`, and says
// so to assistive technology. The URL is the server's, set as the `href` attribute only (React refuses a
// `javascript:` URL there); the text is the caller's.
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export const OUTBOUND_REL = "noopener noreferrer nofollow";

export interface OutboundLinkProps {
  href: string;
  children: ReactNode;
  className?: string;
}

export function OutboundLink({ href, children, className }: OutboundLinkProps) {
  const { t } = useTranslation();
  return (
    <a href={href} target="_blank" rel={OUTBOUND_REL} className={className}>
      {children}{" "}
      <span className="visually-hidden">{t("link.new_tab")}</span>
    </a>
  );
}
