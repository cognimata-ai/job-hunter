import "./styles/tokens.css";
import "./styles/global.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App, createApp } from "./app/App";
import { browserLanguage, initI18n } from "./i18n/index";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element");
}

initI18n(browserLanguage());

createRoot(root).render(
  <StrictMode>
    <App app={createApp()} />
  </StrictMode>,
);
