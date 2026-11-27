const pptxgen = require("pptxgenjs");

// ===== BLEND360 BRAND (extracted from Presentation2 (1).pptx theme1.xml) =====
const NAVY   = "053057"; // dk2 / accent1 — primary dark
const TURQ   = "00EDED"; // accent2 — bright turquoise accent
const MINT   = "A2F3F3"; // accent3 — pale turquoise
const SLATE  = "314550"; // accent4 — secondary dark
const INK    = "0B0D0E"; // accent6 — near-black
const TEAL   = "007676"; // hlink — deep teal
const CREAM  = "F4F3F0"; // lt2 — off-white
const WHITE  = "FFFFFF";
const BLACK  = "000000";
const MUTED_ON_DARK = "AFC2D6"; // navy-tinted light gray for body text on navy
const MUTED_ON_LIGHT = "5B6B7A";

const FONT = "Montserrat";

function mkPres() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5 in
  pres.author = "Blend360";
  pres.company = "Blend360";
  pres.title = "Tableau to Streamlit-in-Snowflake Accelerator";
  return pres;
}

const W = 13.333, H = 7.5;

function bgDark(slide, color) {
  slide.background = { color };
}
function bgLight(slide, color) {
  slide.background = { color: color || WHITE };
}

// Kicker + title header used on every content slide
function header(slide, { kicker, title, dark, sub, pageNum, pageTotal }) {
  const kickerColor = dark ? TURQ : TEAL;
  const titleColor = dark ? WHITE : NAVY;
  const subColor = dark ? MUTED_ON_DARK : MUTED_ON_LIGHT;

  slide.addText(kicker.toUpperCase(), {
    x: 0.55, y: 0.4, w: 8, h: 0.32,
    fontFace: FONT, fontSize: 11, bold: true, color: kickerColor,
    charSpacing: 2,
  });
  slide.addText(title, {
    x: 0.55, y: 0.72, w: 11.6, h: sub ? 0.85 : 1.05,
    fontFace: FONT, fontSize: 26, bold: true, color: titleColor,
    lineSpacingMultiple: 1.05,
  });
  if (sub) {
    slide.addText(sub, {
      x: 0.55, y: sub.length > 90 ? 1.5 : 1.42, w: 11.9, h: 0.5,
      fontFace: FONT, fontSize: 13, color: subColor, italic: false,
    });
  }
  // small brand wordmark, fixed top-right
  slide.addText("BLEND360", {
    x: W - 2.6, y: 0.38, w: 2.1, h: 0.3,
    fontFace: FONT, fontSize: 10, bold: true, color: dark ? TURQ : NAVY,
    align: "right", charSpacing: 2,
  });
  // page number
  if (pageNum) {
    slide.addText(`${String(pageNum).padStart(2, "0")} / ${pageTotal}`, {
      x: W - 2.6, y: H - 0.5, w: 2.1, h: 0.3,
      fontFace: FONT, fontSize: 9, color: dark ? "5D7391" : "9AA7B2",
      align: "right",
    });
  }
}

function iconCircle(slide, { x, y, d = 0.62, glyph, bg, fg, fontSize = 20 }) {
  slide.addShape("ellipse", { x, y, w: d, h: d, fill: { color: bg }, line: { type: "none" } });
  slide.addText(glyph, {
    x, y: y - 0.02, w: d, h: d, align: "center", valign: "middle",
    fontFace: FONT, fontSize, bold: true, color: fg,
  });
}

const TOTAL = 12;

// =====================================================================
const pres = mkPres();

// ---------- SLIDE 1 · COVER ----------
{
  const s = pres.addSlide();
  bgDark(s, NAVY);

  s.addText("BLEND360", {
    x: 0.7, y: 0.55, w: 4, h: 0.35, fontFace: FONT, fontSize: 12, bold: true, color: TURQ, charSpacing: 3,
  });
  s.addText("Data & Analytics · Snowflake Modernization Demo", {
    x: 4.7, y: 0.55, w: 8, h: 0.35, fontFace: FONT, fontSize: 11, color: MUTED_ON_DARK, align: "right",
  });

  s.addText("TABLEAU TO STREAMLIT-IN-SNOWFLAKE", {
    x: 0.7, y: 2.3, w: 11.6, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: TURQ, charSpacing: 2,
  });
  s.addText("Modernizing BI\non Snowflake", {
    x: 0.7, y: 2.75, w: 10.5, h: 1.9, fontFace: FONT, fontSize: 42, bold: true, color: WHITE, lineSpacingMultiple: 1.02,
  });

  // 3 pillars: SIMPLIFY / TRUST / EVOLVE — each with its own distinct description
  const pillars = [
    { t: "SIMPLIFY", d: "Unified BI operating model" },
    { t: "TRUST", d: "Automated migration validation" },
    { t: "EVOLVE", d: "AI-assisted applications" },
  ];
  pillars.forEach((p, i) => {
    const x = 0.7 + i * 3.0;
    const y = 5.05;
    s.addShape("rect", { x, y, w: 0.35, h: 0.06, fill: { color: TURQ }, line: { type: "none" } });
    s.addText(p.t, { x, y: y + 0.16, w: 2.8, h: 0.32, fontFace: FONT, fontSize: 14, bold: true, color: WHITE, charSpacing: 1 });
    s.addText(p.d, { x, y: y + 0.5, w: 2.8, h: 0.5, fontFace: FONT, fontSize: 11, color: MUTED_ON_DARK });
  });

  s.addText(
    "OBJECTIVE  —  Modernize suitable Tableau workloads into governed, AI-ready Streamlit applications in Snowflake while reducing platform duplication and preserving trusted business outcomes.",
    { x: 0.7, y: 6.25, w: 11.6, h: 0.75, fontFace: FONT, fontSize: 11, color: MUTED_ON_DARK, lineSpacingMultiple: 1.25 }
  );
  s.addText("Sharath Kumar  |  Snowflake modernization demo", {
    x: 0.7, y: H - 0.55, w: 6, h: 0.3, fontFace: FONT, fontSize: 9.5, color: "5D7391",
  });

  s.addNotes(
    "Timing: 1 minute. Open with modernization, not tool replacement. The goal is to move suitable BI workloads onto a governed, extensible Snowflake application foundation."
  );
}

// ---------- SLIDE 2 · CURRENT OPERATING MODEL ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "BI Modernization · 02",
    title: "The data platform modernized, but BI still operates separately",
    sub: "Snowflake is the governed system of record; Tableau remains a second platform, control plane and cost base.",
    dark: false, pageNum: 2, pageTotal: TOTAL,
  });

  // Left: current operating model — two boxes + plus
  const boxY = 2.55, boxW = 2.0, boxH = 1.35;
  s.addShape("roundRect", { x: 0.7, y: 2.15, w: 5.6, h: 0.42, fill: { color: CREAM }, line: { type: "none" }, rectRadius: 0.06 });
  s.addText("CURRENT OPERATING MODEL", { x: 0.7, y: 2.15, w: 5.6, h: 0.42, align: "center", valign: "middle", fontFace: FONT, fontSize: 11, bold: true, color: NAVY, charSpacing: 1.5 });

  s.addShape("roundRect", { x: 0.9, y: boxY, w: boxW, h: boxH, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("Snowflake", { x: 0.9, y: boxY + 0.15, w: boxW, h: 0.35, align: "center", fontFace: FONT, fontSize: 14, bold: true, color: WHITE });
  s.addText("Data + governance", { x: 0.9, y: boxY + 0.55, w: boxW, h: 0.35, align: "center", fontFace: FONT, fontSize: 9.5, color: MUTED_ON_DARK });

  s.addText("+", { x: 0.9 + boxW + 0.05, y: boxY + boxH / 2 - 0.25, w: 0.5, h: 0.5, align: "center", fontFace: FONT, fontSize: 22, bold: true, color: MUTED_ON_LIGHT });

  s.addShape("roundRect", { x: 0.9 + boxW + 0.6, y: boxY, w: boxW, h: boxH, fill: { color: SLATE }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("Tableau", { x: 0.9 + boxW + 0.6, y: boxY + 0.15, w: boxW, h: 0.35, align: "center", fontFace: FONT, fontSize: 14, bold: true, color: WHITE });
  s.addText("BI consumption", { x: 0.9 + boxW + 0.6, y: boxY + 0.55, w: boxW, h: 0.35, align: "center", fontFace: FONT, fontSize: 9.5, color: MUTED_ON_DARK });

  s.addText("Two platforms to license, secure, administer, monitor and release.", {
    x: 0.7, y: boxY + boxH + 0.3, w: 5.6, h: 0.8, fontFace: FONT, fontSize: 11.5, italic: true, color: MUTED_ON_LIGHT, lineSpacingMultiple: 1.25,
  });

  s.addShape("roundRect", { x: 0.7, y: 5.75, w: 5.6, h: 1.0, fill: { color: CREAM }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("This is the situation the modernization program is designed to change.", {
    x: 0.95, y: 5.75, w: 5.1, h: 1.0, valign: "middle", fontFace: FONT, fontSize: 12, bold: true, color: NAVY, lineSpacingMultiple: 1.2,
  });

  // Right: THE RESULT — 5 icon rows
  s.addText("THE RESULT", { x: 6.85, y: 2.15, w: 5.8, h: 0.32, fontFace: FONT, fontSize: 11, bold: true, color: TEAL, charSpacing: 1.5 });

  const results = [
    { g: "$", t: "Separate cost", d: "Tableau licenses, infrastructure or cloud subscription" },
    { g: "⚙", t: "Duplicated operations", d: "Administration, access, monitoring and deployment" },
    { g: "⇄", t: "Data movement", d: "Extract creation, refresh schedules and failure handling" },
    { g: "∑", t: "Logic duplication", d: "Business calculations repeated across ELT and dashboards" },
    { g: "⊕", t: "Limited extensibility", d: "Additional applications needed beyond dashboard interaction" },
  ];
  results.forEach((r, i) => {
    const y = 2.6 + i * 0.85;
    iconCircle(s, { x: 6.85, y, d: 0.52, glyph: r.g, bg: NAVY, fg: TURQ, fontSize: 16 });
    s.addText(r.t, { x: 7.55, y: y - 0.03, w: 5.1, h: 0.32, fontFace: FONT, fontSize: 12.5, bold: true, color: NAVY });
    s.addText(r.d, { x: 7.55, y: y + 0.27, w: 5.1, h: 0.48, fontFace: FONT, fontSize: 9.5, color: MUTED_ON_LIGHT, lineSpacingMultiple: 1.1 });
  });

  s.addNotes(
    "Timing: 3 minutes. Explain the situation before mentioning the accelerator. Do not attack Tableau; describe the operational duplication created when the data platform and BI platform evolve separately. Replace generic cost language with client figures when available."
  );
}

// ---------- SLIDE 3 · WHY STREAMLIT IN SNOWFLAKE, NOT PLAIN STREAMLIT ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "BI Modernization · 03",
    title: "Why Streamlit in Snowflake — not a standalone Streamlit app",
    sub: "Same open-source framework. The platform underneath it is what changes the answer.",
    dark: false, pageNum: 3, pageTotal: TOTAL,
  });

  const cols = [
    { g: "⛨", t: "No egress, no copies", d: "Compute runs next to the data. A standalone app pulls governed data out of the boundary on every query." },
    { g: "⚿", t: "Governance inherited", d: "Runs as a Snowflake role. RBAC, row-access, masking and audit apply automatically — nothing rebuilt in a second environment." },
    { g: "⚙", t: "Zero infrastructure", d: "Serverless inside Snowflake. No VM, container, patching or scaling for anyone to own." },
    { g: "✦", t: "Cortex is a local call", d: "COMPLETE, Analyst and semantic views execute in-account. Outside Snowflake, using Cortex means calling back into the platform you just left." },
  ];
  const cardW = 2.78, gap = 0.22, startX = 0.55, cardY = 2.35, cardH = 3.15;
  cols.forEach((c, i) => {
    const x = startX + i * (cardW + gap);
    s.addShape("roundRect", { x, y: cardY, w: cardW, h: cardH, fill: { color: CREAM }, line: { type: "none" }, rectRadius: 0.07 });
    iconCircle(s, { x: x + 0.28, y: cardY + 0.28, d: 0.6, glyph: c.g, bg: NAVY, fg: TURQ, fontSize: 19 });
    s.addText(c.t, { x: x + 0.28, y: cardY + 1.05, w: cardW - 0.55, h: 0.75, fontFace: FONT, fontSize: 13, bold: true, color: NAVY, lineSpacingMultiple: 1.1 });
    s.addText(c.d, { x: x + 0.28, y: cardY + 1.75, w: cardW - 0.55, h: 1.3, fontFace: FONT, fontSize: 9.5, color: MUTED_ON_LIGHT, lineSpacingMultiple: 1.25 });
  });

  s.addShape("roundRect", { x: 0.55, y: 5.75, w: 12.25, h: 1.15, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("THE HONEST BOUNDARY", { x: 0.85, y: 5.9, w: 11.65, h: 0.3, fontFace: FONT, fontSize: 10, bold: true, color: TURQ, charSpacing: 1.5 });
  s.addText(
    "If the data weren't already in Snowflake, and there were no governance or AI requirement, a standalone Streamlit app would be the simpler choice. The case rests on data-in-Snowflake + governance + Cortex — exactly this situation.",
    { x: 0.85, y: 6.18, w: 11.65, h: 0.65, fontFace: FONT, fontSize: 10.5, italic: true, color: WHITE, lineSpacingMultiple: 1.2 }
  );

  s.addNotes(
    "Timing: 2 minutes. This slide exists to pre-empt the question every technical Snowflake audience asks: why not just Streamlit on its own? Lead with the honest boundary — it builds credibility precisely because it isn't a universal claim. The case is data-in-Snowflake + governance + Cortex, not 'Streamlit is better in Snowflake' as a blanket statement."
  );
}

// ---------- SLIDE 4 · VALUE BEYOND VISUALIZATION ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "BI Modernization · 04",
    title: "Modernization creates value beyond visualization",
    sub: "The opportunity is larger than replacing a visualization tool.",
    dark: false, pageNum: 4, pageTotal: TOTAL,
  });

  const cols = [
    { g: "⇳", t: "Platform economics", h: "Lower duplicate cost", d: "Potentially reduce Tableau licenses and duplicated platform support for migrated users." },
    { g: "⚡", t: "Delivery speed", h: "Shorter change cycles", d: "Build on governed Snowflake data using reusable Python and Streamlit components." },
    { g: "⚖", t: "Governance", h: "Fewer control gaps", d: "Reuse Snowflake RBAC, masking policies, semantic assets and controlled deployment." },
    { g: "✦", t: "AI-ready experiences", h: "New client experiences", d: "Extend migrated apps with CoCo and add natural-language analytics through Cortex Analyst." },
  ];
  const cardW = 2.78, gap = 0.22, startX = 0.55, cardY = 2.35, cardH = 3.55;
  cols.forEach((c, i) => {
    const x = startX + i * (cardW + gap);
    s.addShape("roundRect", { x, y: cardY, w: cardW, h: cardH, fill: { color: CREAM }, line: { type: "none" }, rectRadius: 0.07 });
    iconCircle(s, { x: x + 0.28, y: cardY + 0.32, d: 0.62, glyph: c.g, bg: NAVY, fg: TURQ, fontSize: 20 });
    s.addText(c.t, { x: x + 0.28, y: cardY + 1.12, w: cardW - 0.55, h: 0.6, fontFace: FONT, fontSize: 14, bold: true, color: NAVY, lineSpacingMultiple: 1.05 });
    s.addText(c.h.toUpperCase(), { x: x + 0.28, y: cardY + 1.75, w: cardW - 0.55, h: 0.3, fontFace: FONT, fontSize: 10, bold: true, color: TEAL, charSpacing: 0.8 });
    s.addText(c.d, { x: x + 0.28, y: cardY + 2.1, w: cardW - 0.55, h: 1.3, fontFace: FONT, fontSize: 10, color: MUTED_ON_LIGHT, lineSpacingMultiple: 1.25 });
  });

  s.addShape("roundRect", { x: 0.55, y: 6.15, w: 12.25, h: 0.75, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("Measure the business case  —  baseline licenses, platform support, extract operations and dashboard change lead time before the pilot.", {
    x: 0.85, y: 6.15, w: 11.7, h: 0.75, valign: "middle", fontFace: FONT, fontSize: 11.5, bold: true, color: WHITE,
  });

  s.addNotes(
    "Timing: 3 minutes. Leadership should hear business outcomes; Snowflake should hear platform consolidation; BI developers should hear faster iteration and extensibility. License savings are potential until client-specific TCO is calculated."
  );
}

// ---------- SLIDE 5 · AI-ASSISTED BI LIFECYCLE ----------
{
  const s = pres.addSlide();
  bgDark(s, NAVY);
  header(s, {
    kicker: "AI-Assisted BI Lifecycle · 05",
    title: "The migrated application becomes an AI-assisted product, not a static replica",
    sub: "Natural-language enhancement changes the economics of maintaining and extending BI applications.",
    dark: true, pageNum: 5, pageTotal: TOTAL,
  });
  s.addShape("roundRect", { x: 10.6, y: 0.4, w: 2.15, h: 0.32, fill: { color: SLATE }, line: { color: TURQ, width: 0.75 }, rectRadius: 0.16 });
  s.addText("VISION · SNOWFLAKE COCO", { x: 10.6, y: 0.4, w: 2.15, h: 0.32, align: "center", valign: "middle", fontFace: FONT, fontSize: 7.5, bold: true, color: TURQ, charSpacing: 0.5 });

  // 5-step lifecycle with connecting line
  const steps = ["Migrate", "Validate", "Deploy", "Enhance", "Revalidate"];
  const subs = ["Tableau to\nStreamlit", "Visual, data,\nformulas", "Governed\nSiS app", "CoCo\ninstructions", "Controlled\nrelease"];
  const stepY = 2.5, stepD = 0.62, n = steps.length;
  const stepStartX = 1.0, stepEndX = 11.3;
  const stepGap = (stepEndX - stepStartX) / (n - 1);
  s.addShape("line", { x: stepStartX + stepD / 2, y: stepY + stepD / 2, w: stepEndX - stepStartX - stepD, h: 0, line: { color: "2C4D71", width: 1.5 } });
  steps.forEach((t, i) => {
    const x = stepStartX + i * stepGap;
    iconCircle(s, { x, y: stepY, d: stepD, glyph: String(i + 1), bg: i === 3 ? TURQ : "1A3A5C", fg: i === 3 ? NAVY : TURQ, fontSize: 18 });
    s.addText(t, { x: x - 0.55, y: stepY + 0.72, w: stepD + 1.1, h: 0.32, align: "center", fontFace: FONT, fontSize: 12, bold: true, color: WHITE });
    s.addText(subs[i], { x: x - 0.55, y: stepY + 1.04, w: stepD + 1.1, h: 0.55, align: "center", fontFace: FONT, fontSize: 8.5, color: MUTED_ON_DARK, lineSpacingMultiple: 1.1 });
  });

  // CoCo example instruction card
  s.addShape("roundRect", { x: 0.7, y: 4.55, w: 6.1, h: 2.15, fill: { color: SLATE }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("COCO / CORTEX CODE", { x: 0.95, y: 4.72, w: 5.6, h: 0.3, fontFace: FONT, fontSize: 10, bold: true, color: TURQ, charSpacing: 1 });
  s.addText("Generates reviewable application changes from natural-language requirements.", {
    x: 0.95, y: 5.0, w: 5.6, h: 0.45, fontFace: FONT, fontSize: 10.5, color: MUTED_ON_DARK, lineSpacingMultiple: 1.2,
  });
  s.addShape("roundRect", { x: 0.95, y: 5.5, w: 5.6, h: 1.05, fill: { color: NAVY }, line: { color: "2C4D71", width: 0.75 }, rectRadius: 0.06 });
  s.addText("“Add a region filter to every chart, preserve existing calculations, and create a customer-detail view on selection.”", {
    x: 1.15, y: 5.5, w: 5.2, h: 1.05, valign: "middle", italic: true, fontFace: FONT, fontSize: 10.5, color: WHITE, lineSpacingMultiple: 1.2,
  });

  // Cortex Analyst card
  s.addShape("roundRect", { x: 7.05, y: 4.55, w: 5.55, h: 2.15, fill: { color: SLATE }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("CORTEX ANALYST", { x: 7.3, y: 4.72, w: 5.05, h: 0.3, fontFace: FONT, fontSize: 10, bold: true, color: TURQ, charSpacing: 1 });
  s.addText("Adds governed natural-language questions over semantic views.", {
    x: 7.3, y: 5.0, w: 5.05, h: 0.5, fontFace: FONT, fontSize: 10.5, color: MUTED_ON_DARK, lineSpacingMultiple: 1.2,
  });
  s.addText("Every AI-assisted change returns through regression and validation before release — CoCo lowers dependence on specialized coding skills, but production changes are never unreviewed.", {
    x: 7.3, y: 5.55, w: 5.05, h: 1.0, fontFace: FONT, fontSize: 9.5, italic: true, color: MUTED_ON_DARK, lineSpacingMultiple: 1.25,
  });

  s.addNotes(
    "Timing: 3 minutes. This is a primary modernization advantage, but say clearly this is where the roadmap is headed next, not a capability wired into the accelerator's current five stages. Say that CoCo lowers dependence on specialized coding skills; do not promise that production applications can be changed without engineering review. Every AI-assisted change returns through regression and validation."
  );
}

// ---------- SLIDE 6 · FIVE CONTROLLED STAGES ----------
{
  const s = pres.addSlide();
  bgDark(s, NAVY);
  header(s, {
    kicker: "Accelerator Demo · 06",
    title: "One workbook moves through five controlled stages",
    sub: "Each stage leaves an inspectable artifact before the next stage proceeds.",
    dark: true, pageNum: 6, pageTotal: TOTAL,
  });

  const stages = [
    { n: "01", t: "Discovery", d: "Sources, extracts\nand tables" },
    { n: "02", t: "Parsing", d: "Workbook to\nmigration IR" },
    { n: "03", t: "Data model", d: "Relations and optional\nsemantic view" },
    { n: "04", t: "App creation", d: "Generated Streamlit\napplication" },
    { n: "05", t: "Validation", d: "Visual, data and\nformula evidence" },
  ];
  const cardW = 2.2, gap = 0.18, startX = 0.65, cardY = 2.5, cardH = 2.85;
  stages.forEach((st, i) => {
    const x = startX + i * (cardW + gap);
    const active = i === 4;
    s.addShape("roundRect", { x, y: cardY, w: cardW, h: cardH, fill: { color: active ? TURQ : SLATE }, line: { type: "none" }, rectRadius: 0.08 });
    s.addText(st.n, { x: x + 0.18, y: cardY + 0.18, w: cardW - 0.36, h: 0.5, fontFace: FONT, fontSize: 22, bold: true, color: active ? NAVY : "3F6C93" });
    s.addText(st.t, { x: x + 0.18, y: cardY + 0.75, w: cardW - 0.36, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: active ? NAVY : WHITE });
    s.addText(st.d, { x: x + 0.18, y: cardY + 1.2, w: cardW - 0.36, h: 0.85, fontFace: FONT, fontSize: 9.5, color: active ? "0B3A52" : MUTED_ON_DARK, lineSpacingMultiple: 1.15 });
    if (i < 4) {
      s.addText("→", { x: x + cardW - 0.02, y: cardY + cardH / 2 - 0.25, w: 0.4, h: 0.5, align: "center", fontFace: FONT, fontSize: 20, bold: true, color: TURQ });
    }
  });

  s.addShape("roundRect", { x: 0.65, y: 5.7, w: 12.05, h: 0.95, fill: { color: SLATE }, line: { color: TURQ, width: 1 }, rectRadius: 0.08 });
  s.addText("⛓  Human deployment gate", { x: 0.95, y: 5.7, w: 3.4, h: 0.95, valign: "middle", fontFace: FONT, fontSize: 12.5, bold: true, color: TURQ });
  s.addText("The application is deployed only after findings and validation are visible.", {
    x: 4.3, y: 5.7, w: 8.2, h: 0.95, valign: "middle", fontFace: FONT, fontSize: 11.5, color: WHITE,
  });

  s.addNotes(
    "Timing: 3 minutes. This slide replaces the conflicting three-stage and seven-stage descriptions in earlier material. During the live demo, narrate the same five labels shown here."
  );
}

// ---------- SLIDE 7 · PLATFORM ARCHITECTURE (real system diagram) ----------
{
  const s = pres.addSlide();
  bgDark(s, "0a1428"); // matches the diagram's own canvas exactly, no visible seam
  header(s, {
    kicker: "Accelerator Demo · 07",
    title: "Platform architecture",
    dark: true, pageNum: 7, pageTotal: TOTAL,
  });
  s.addText(
    "One engine runs once per workbook across the estate; Cortex sits inside the account; three independent validations before anything is approved.",
    { x: 0.55, y: 1.55, w: 12.2, h: 0.4, fontFace: FONT, fontSize: 12, color: MUTED_ON_DARK }
  );

  // Real system diagram, rendered from the platform-architecture artifact (1720 x 1066)
  // Given its own dedicated band below the header — no sub-line competing for the same space,
  // centered on both axes with equal margins so it never reads as pushed into a corner.
  const imgAspect = 1720 / 1066;
  const imgTop = 2.1, imgBottom = 7.15;
  const imgH = imgBottom - imgTop, imgWraw = imgH * imgAspect;
  const imgW = Math.min(imgWraw, W - 1.1);
  const imgHfinal = imgW < imgWraw ? imgW / imgAspect : imgH;
  const imgX = (W - imgW) / 2, imgY = imgTop + (imgH - imgHfinal) / 2;
  s.addImage({ path: "architecture.png", x: imgX, y: imgY, w: imgW, h: imgHfinal });

  s.addNotes(
    "Timing: 2 minutes. Walk left to right: ingestion/discovery, conversion into an IR, Cortex called from inside the account (proposes only, never final say), semantic and serving layer, then the three-lane QA gate (data / calc / visual) before anything ships. This is the detailed technical backing for the five-stage story just shown."
  );
}

// ---------- SLIDE 8 · VISUAL PARITY ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "Accelerator Demo · 08",
    title: "Visual parity is reviewed at dashboard level",
    sub: "The first check is immediate: does the migrated tab preserve the same analytical composition?",
    dark: false, pageNum: 8, pageTotal: TOTAL,
  });

  // Compare placeholder — two panes with a VS divider, real screenshots go here
  const paneY = 2.5, paneH = 3.15, paneW = 5.55;
  [{ x: 0.7, label: "TABLEAU" }, { x: 7.1, label: "STREAMLIT IN SNOWFLAKE" }].forEach((p) => {
    s.addShape("roundRect", { x: p.x, y: paneY, w: paneW, h: paneH, fill: { color: CREAM }, line: { color: "D8D6D0", width: 1 }, rectRadius: 0.06 });
    s.addText(p.label, { x: p.x, y: paneY + 0.15, w: paneW, h: 0.3, align: "center", fontFace: FONT, fontSize: 10, bold: true, color: MUTED_ON_LIGHT, charSpacing: 1.5 });
    s.addText("[ Insert dashboard screenshot ]", { x: p.x, y: paneY + paneH / 2 - 0.2, w: paneW, h: 0.4, align: "center", fontFace: FONT, fontSize: 11, italic: true, color: "B7BAC0" });
  });
  iconCircle(s, { x: W / 2 - 0.33, y: paneY + paneH / 2 - 0.33, d: 0.66, glyph: "=", bg: NAVY, fg: TURQ, fontSize: 22 });

  // evidence chip row
  const chips = ["layout hierarchy", "KPI presence", "chart type", "filters", "labels", "visible grain"];
  let cx = 0.7;
  const chipY = 5.95;
  chips.forEach((c) => {
    const cw = 0.35 + c.length * 0.1;
    s.addShape("roundRect", { x: cx, y: chipY, w: cw, h: 0.42, fill: { color: CREAM }, line: { color: TEAL, width: 0.75 }, rectRadius: 0.21 });
    s.addText(c, { x: cx, y: chipY, w: cw, h: 0.42, align: "center", valign: "middle", fontFace: FONT, fontSize: 9.5, bold: true, color: TEAL });
    cx += cw + 0.18;
  });

  s.addText("Review evidence  —  visual similarity alone is not a pass; it is the first evidence layer.", {
    x: 0.7, y: 6.6, w: 11.9, h: 0.5, fontFace: FONT, fontSize: 11, italic: true, color: MUTED_ON_LIGHT,
  });

  s.addNotes(
    "Timing: 2 minutes. Visual similarity alone is not a pass. It is the first evidence layer. Point out that the chart colors can differ while data, hierarchy and interaction behavior still require independent checks. Replace the placeholder panes with the real Tableau vs Streamlit comparison screenshot before presenting."
  );
}

// ---------- SLIDE 9 · DATA + FORMULA PROOF ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "Accelerator Demo · 09",
    title: "A PASS is earned with chart-grain numbers and formulas",
    sub: "The displayed visual grain determines the evidence: product rows for a ranked product table, not a dashboard total.",
    dark: false, pageNum: 9, pageTotal: TOTAL,
  });

  s.addText("DATA PROOF", { x: 0.7, y: 2.25, w: 6, h: 0.3, fontFace: FONT, fontSize: 11, bold: true, color: TEAL, charSpacing: 1.5 });

  const rows = [
    ["Product", "Tableau", "Streamlit", "Backend", "Diff", "Result"],
    ["Canon imageCLASS 2200", "$61,600.00", "$61,599.82", "$61,599.82", "$0.18", "PASS"],
    ["Fellowes PB500", "$27,454.00", "$27,453.78", "$27,453.78", "$0.22", "PASS"],
    ["Cisco TelePresence", "$22,638.00", "$22,638.28", "$22,638.28", "$0.28", "PASS"],
  ];
  const tRows = rows.map((r, ri) =>
    r.map((c, ci) => ({
      text: c,
      options: {
        fontFace: FONT,
        fontSize: 10.5,
        bold: ri === 0,
        color: ri === 0 ? WHITE : ci === 5 ? "1E8449" : NAVY,
        fill: { color: ri === 0 ? NAVY : ri % 2 === 0 ? CREAM : WHITE },
        align: ci === 0 ? "left" : "center",
        valign: "middle",
      },
    }))
  );
  s.addTable(tRows, {
    x: 0.7, y: 2.6, w: 7.3, h: 1.7,
    colW: [2.5, 1.15, 1.15, 1.15, 0.7, 0.85],
    border: { type: "solid", color: "E5E3DE", pt: 0.5 },
    autoPage: false,
  });
  s.addText("The three displayed rows are representative slide evidence; the downloadable report retains the complete result set and every mismatch.", {
    x: 0.7, y: 4.45, w: 7.3, h: 0.55, fontFace: FONT, fontSize: 9.5, italic: true, color: MUTED_ON_LIGHT, lineSpacingMultiple: 1.2,
  });

  // Formula proof
  s.addText("FORMULA PROOF", { x: 8.35, y: 2.25, w: 4.3, h: 0.3, fontFace: FONT, fontSize: 11, bold: true, color: TEAL, charSpacing: 1.5 });
  s.addShape("roundRect", { x: 8.35, y: 2.6, w: 4.3, h: 2.0, fill: { color: CREAM }, line: { type: "none" }, rectRadius: 0.07 });
  s.addText("Profit Ratio", { x: 8.6, y: 2.75, w: 3.8, h: 0.32, fontFace: FONT, fontSize: 11.5, bold: true, color: NAVY });
  s.addText("TABLEAU", { x: 8.6, y: 3.12, w: 3.8, h: 0.24, fontFace: FONT, fontSize: 8.5, bold: true, color: MUTED_ON_LIGHT, charSpacing: 1 });
  s.addText("SUM([Profit]) / SUM([Sales])", { x: 8.6, y: 3.35, w: 3.8, h: 0.35, fontFace: "Courier New", fontSize: 10, color: NAVY });
  s.addText("SNOWFLAKE SQL", { x: 8.6, y: 3.78, w: 3.8, h: 0.24, fontFace: FONT, fontSize: 8.5, bold: true, color: MUTED_ON_LIGHT, charSpacing: 1 });
  s.addText("SUM(PROFIT) / NULLIF(SUM(SALES), 0)", { x: 8.6, y: 4.0, w: 3.8, h: 0.35, fontFace: "Courier New", fontSize: 9.5, color: TEAL });
  s.addShape("roundRect", { x: 8.6, y: 4.4, w: 3.8, h: 0.32, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.16 });
  s.addText("Semantically equivalent + null-safe", { x: 8.6, y: 4.4, w: 3.8, h: 0.32, align: "center", valign: "middle", fontFace: FONT, fontSize: 8.5, bold: true, color: TURQ });

  s.addShape("roundRect", { x: 0.7, y: 5.35, w: 11.95, h: 1.35, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("Absolute and relative tolerance are recorded in the downloadable report. Currency tolerance is expressed in currency, not points.", {
    x: 1.0, y: 5.35, w: 11.35, h: 1.35, valign: "middle", fontFace: FONT, fontSize: 12, italic: true, color: WHITE, lineSpacingMultiple: 1.25,
  });

  s.addNotes(
    "Timing: 4 minutes. The three displayed rows are representative slide evidence; the downloadable report retains the complete result set and every mismatch. Explain absolute and relative tolerance in the report and that currency tolerance must be expressed in currency, not points."
  );
}

// ---------- SLIDE 10 · NEXT STEPS (partner-facing close) ----------
{
  const s = pres.addSlide();
  bgDark(s, NAVY);
  header(s, {
    kicker: "Next Steps · 10  ·  Core presentation ends here",
    title: "Let's find the right proof point together",
    sub: "A proposed path from this demo to a joint customer win on Snowflake.",
    dark: true, pageNum: 10, pageTotal: TOTAL,
  });

  const steps = [
    { n: "01", t: "Identify a workload", d: "A Snowflake account team nominates one Tableau-on-Snowflake workload as the proof point." },
    { n: "02", t: "Run it live", d: "The migration executes inside that account, stage by stage, with the account team watching." },
    { n: "03", t: "Review together", d: "Visual, data and formula evidence reviewed jointly before anyone calls it a win." },
  ];
  const cw = 3.75, gap = 0.25, startX = 0.65, cy = 2.55, ch = 2.1;
  steps.forEach((st, i) => {
    const x = startX + i * (cw + gap);
    s.addShape("roundRect", { x, y: cy, w: cw, h: ch, fill: { color: SLATE }, line: { type: "none" }, rectRadius: 0.08 });
    iconCircle(s, { x: x + 0.25, y: cy + 0.25, d: 0.55, glyph: st.n, bg: TURQ, fg: NAVY, fontSize: 15 });
    s.addText(st.t, { x: x + 0.25, y: cy + 0.95, w: cw - 0.5, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: WHITE });
    s.addText(st.d, { x: x + 0.25, y: cy + 1.35, w: cw - 0.5, h: 0.65, fontFace: FONT, fontSize: 10, color: MUTED_ON_DARK, lineSpacingMultiple: 1.25 });
    if (i < 2) s.addText("→", { x: x + cw - 0.02, y: cy + ch / 2 - 0.2, w: 0.35, h: 0.4, align: "center", fontFace: FONT, fontSize: 18, bold: true, color: TURQ });
  });

  s.addShape("roundRect", { x: 0.65, y: 5.15, w: 12.05, h: 1.2, fill: { color: "0A4360" }, line: { color: TURQ, width: 1.25 }, rectRadius: 0.08 });
  s.addText("WHAT WE'RE ASKING OF SNOWFLAKE", { x: 0.95, y: 5.32, w: 11.4, h: 0.3, fontFace: FONT, fontSize: 11, bold: true, color: TURQ, charSpacing: 1.5 });
  s.addText("Help us identify one workload and one account team ready to run this live.", {
    x: 0.95, y: 5.62, w: 11.4, h: 0.6, fontFace: FONT, fontSize: 14, bold: true, color: WHITE,
  });

  s.addNotes(
    "Timing: 2 minutes. Close on a concrete ask to the Snowflake partner audience, not a generic next-step list. This is a co-sell motion: we need Snowflake to nominate the account and workload."
  );
}

// ===================== APPENDIX =====================

// ---------- SLIDE 11 · APPENDIX: CORTEX BOUNDARY ----------
{
  const s = pres.addSlide();
  bgDark(s, NAVY);
  s.addShape("roundRect", { x: 0.55, y: 0.4, w: 1.55, h: 0.34, fill: { color: TURQ }, line: { type: "none" }, rectRadius: 0.06 });
  s.addText("APPENDIX", { x: 0.55, y: 0.4, w: 1.55, h: 0.34, align: "center", valign: "middle", fontFace: FONT, fontSize: 9.5, bold: true, color: NAVY, charSpacing: 1 });
  header(s, {
    kicker: "",
    title: "Appendix: Cortex assists only where its output can be governed",
    dark: true, pageNum: 11, pageTotal: TOTAL,
  });

  // Deterministic core column
  s.addShape("roundRect", { x: 0.7, y: 2.3, w: 5.7, h: 3.9, fill: { color: SLATE }, line: { type: "none" }, rectRadius: 0.08 });
  s.addText("DETERMINISTIC CORE", { x: 1.0, y: 2.5, w: 5.1, h: 0.35, fontFace: FONT, fontSize: 12, bold: true, color: WHITE, charSpacing: 1 });
  const det = ["Workbook parser", "Intermediate representation", "Calculation rules", "Code generation", "Numeric validation"];
  det.forEach((d, i) => {
    const y = 3.05 + i * 0.58;
    iconCircle(s, { x: 1.0, y, d: 0.4, glyph: String(i + 1), bg: NAVY, fg: TURQ, fontSize: 13 });
    s.addText(d, { x: 1.55, y: y + 0.02, w: 4.6, h: 0.38, valign: "middle", fontFace: FONT, fontSize: 12, color: WHITE });
  });
  s.addText("No model decision is needed for the standard path.", {
    x: 1.0, y: 5.85, w: 5.1, h: 0.3, italic: true, fontFace: FONT, fontSize: 9.5, color: MUTED_ON_DARK,
  });

  // plus sign
  s.addText("+", { x: 6.45, y: 3.9, w: 0.5, h: 0.5, align: "center", fontFace: FONT, fontSize: 24, bold: true, color: TURQ });

  // Optional Cortex column
  s.addShape("roundRect", { x: 6.95, y: 2.3, w: 5.7, h: 3.9, fill: { color: "0A4360" }, line: { color: TURQ, width: 1 }, rectRadius: 0.08 });
  s.addText("OPTIONAL CORTEX ASSISTANCE", { x: 7.25, y: 2.5, w: 5.1, h: 0.35, fontFace: FONT, fontSize: 12, bold: true, color: TURQ, charSpacing: 1 });
  const opt = ["Semantic view generation", "Guarded calculation proposal", "Narrative validation summary"];
  opt.forEach((d, i) => {
    const y = 3.05 + i * 0.58;
    iconCircle(s, { x: 7.25, y, d: 0.4, glyph: "✦", bg: TURQ, fg: NAVY, fontSize: 13 });
    s.addText(d, { x: 7.8, y: y + 0.02, w: 4.6, h: 0.38, valign: "middle", fontFace: FONT, fontSize: 12, color: WHITE });
  });
  ["Execution-gated", "Human-reviewed"].forEach((t, i) => {
    const y = 4.85 + i * 0.42;
    s.addShape("roundRect", { x: 7.25, y, w: 2.15, h: 0.34, fill: { color: NAVY }, line: { type: "none" }, rectRadius: 0.17 });
    s.addText(t, { x: 7.25, y, w: 2.15, h: 0.34, align: "center", valign: "middle", fontFace: FONT, fontSize: 9, bold: true, color: TURQ });
  });
  s.addText("Cortex cannot convert a deterministic mismatch into a PASS.", {
    x: 7.25, y: 5.75, w: 5.1, h: 0.4, italic: true, fontFace: FONT, fontSize: 9.5, color: MUTED_ON_DARK, lineSpacingMultiple: 1.2,
  });

  s.addNotes(
    "Use when Snowflake asks where Cortex adds value. The concise answer: Cortex enriches the governed semantic experience and can propose translations for unsupported calculations, but deterministic checks retain authority."
  );
}

// ---------- SLIDE 12 · APPENDIX: EVIDENCE CONTRACT ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  s.addShape("roundRect", { x: 0.55, y: 0.4, w: 1.55, h: 0.34, fill: { color: SLATE }, line: { type: "none" }, rectRadius: 0.06 });
  s.addText("APPENDIX", { x: 0.55, y: 0.4, w: 1.55, h: 0.34, align: "center", valign: "middle", fontFace: FONT, fontSize: 9.5, bold: true, color: WHITE, charSpacing: 1 });
  header(s, {
    kicker: "",
    title: "Appendix: every dashboard tab receives the same evidence contract",
    dark: false, pageNum: 12, pageTotal: TOTAL,
  });

  const items = [
    { n: "1", t: "Summary", d: "Dashboard and sheet verdicts with reasons" },
    { n: "2", t: "Visual", d: "Tableau and Streamlit tab comparison when source capture is available" },
    { n: "3", t: "Chart data", d: "Every displayed key compared across Tableau, Streamlit and backend" },
    { n: "4", t: "Formulas", d: "Tableau calculation mapped to generated Snowflake SQL" },
    { n: "5", t: "Interactions", d: "Filters, parameters, sorting and tooltip behavior checked" },
    { n: "6", t: "Artifacts", d: "Full rows, mismatches, queries and notebook retained for audit" },
  ];
  const cols = 3, cw = 3.85, ch = 1.9, gx = 0.2, gy = 0.25, startX = 0.7, startY = 2.35;
  items.forEach((it, i) => {
    const col = i % cols, row = Math.floor(i / cols);
    const x = startX + col * (cw + gx), y = startY + row * (ch + gy);
    s.addShape("roundRect", { x, y, w: cw, h: ch, fill: { color: CREAM }, line: { type: "none" }, rectRadius: 0.07 });
    iconCircle(s, { x: x + 0.25, y: y + 0.25, d: 0.55, glyph: it.n, bg: NAVY, fg: TURQ, fontSize: 18 });
    s.addText(it.t, { x: x + 0.95, y: y + 0.25, w: cw - 1.15, h: 0.55, valign: "middle", fontFace: FONT, fontSize: 14, bold: true, color: NAVY });
    s.addText(it.d, { x: x + 0.25, y: y + 0.95, w: cw - 0.5, h: 0.85, fontFace: FONT, fontSize: 10, color: MUTED_ON_LIGHT, lineSpacingMultiple: 1.2 });
  });

  s.addText("The HTML report is the readable entry point; the notebook and row-level artifacts provide the complete proof set.", {
    x: 0.7, y: 6.6, w: 11.95, h: 0.4, fontFace: FONT, fontSize: 10.5, italic: true, color: MUTED_ON_LIGHT,
  });

  s.addNotes(
    "Use when the audience asks what is inside the generated report. Stress that the HTML is the readable entry point while the notebook and row-level artifacts provide the complete proof set."
  );
}

pres.writeFile({ fileName: "Tableau_to_Streamlit_in_Snowflake_Accelerator_Demo_BLEND.pptx" })
  .then(() => console.log("DONE"))
  .catch((e) => { console.error(e); process.exit(1); });
