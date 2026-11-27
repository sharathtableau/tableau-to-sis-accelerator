import re
path = r"C:\Users\SharathKumarKammari\Downloads\Tableau to SiS_Cowork - Cortex\blend_analysis\build_deck.js"
s = open(path, encoding="utf-8").read()

s = s.replace('const TOTAL = 11;', 'const TOTAL = 12;')

s = s.replace(
'''// ---------- SLIDE 4 · VALUE BEYOND VISUALIZATION ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "BI Modernization · 03",
    title: "Modernization creates value beyond visualization",
    sub: "The opportunity is larger than replacing a visualization tool.",
    dark: false, pageNum: 3, pageTotal: TOTAL,''',
'''// ---------- SLIDE 4 · VALUE BEYOND VISUALIZATION ----------
{
  const s = pres.addSlide();
  bgLight(s, WHITE);
  header(s, {
    kicker: "BI Modernization · 04",
    title: "Modernization creates value beyond visualization",
    sub: "The opportunity is larger than replacing a visualization tool.",
    dark: false, pageNum: 4, pageTotal: TOTAL,''')

s = s.replace('// ---------- SLIDE 4 · AI-ASSISTED BI LIFECYCLE ----------', '// ---------- SLIDE 5 · AI-ASSISTED BI LIFECYCLE ----------')
s = s.replace('kicker: "AI-Assisted BI Lifecycle · 04",', 'kicker: "AI-Assisted BI Lifecycle · 05",')
s = s.replace('dark: true, pageNum: 4, pageTotal: TOTAL,', 'dark: true, pageNum: 5, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 5 · FIVE CONTROLLED STAGES ----------', '// ---------- SLIDE 6 · FIVE CONTROLLED STAGES ----------')
s = s.replace('kicker: "Accelerator Demo · 05",', 'kicker: "Accelerator Demo · 06",')
s = s.replace('dark: true, pageNum: 5, pageTotal: TOTAL,', 'dark: true, pageNum: 6, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 6 · VISUAL PARITY ----------', '// ---------- SLIDE 7 · VISUAL PARITY ----------')
s = s.replace('kicker: "Accelerator Demo · 06",', 'kicker: "Accelerator Demo · 07",')
s = s.replace('dark: false, pageNum: 6, pageTotal: TOTAL,', 'dark: false, pageNum: 7, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 7 · DATA + FORMULA PROOF ----------', '// ---------- SLIDE 8 · DATA + FORMULA PROOF ----------')
s = s.replace('kicker: "Accelerator Demo · 07",', 'kicker: "Accelerator Demo · 08",')
s = s.replace('dark: false, pageNum: 7, pageTotal: TOTAL,', 'dark: false, pageNum: 8, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 8 · NEXT STEPS (partner-facing close) ----------', '// ---------- SLIDE 9 · NEXT STEPS (partner-facing close) ----------')
s = s.replace('kicker: "Next Steps · 08  ·  Core presentation ends here",', 'kicker: "Next Steps · 09  ·  Core presentation ends here",')
s = s.replace('dark: true, pageNum: 8, pageTotal: TOTAL,', 'dark: true, pageNum: 9, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 9 · APPENDIX: CORTEX BOUNDARY ----------', '// ---------- SLIDE 10 · APPENDIX: CORTEX BOUNDARY ----------')
s = s.replace('dark: true, pageNum: 9, pageTotal: TOTAL,', 'dark: true, pageNum: 10, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 10 · APPENDIX: EVIDENCE CONTRACT ----------', '// ---------- SLIDE 11 · APPENDIX: EVIDENCE CONTRACT ----------')
s = s.replace('dark: false, pageNum: 10, pageTotal: TOTAL,', 'dark: false, pageNum: 11, pageTotal: TOTAL,')

s = s.replace('// ---------- SLIDE 11 · APPENDIX: ARCHITECTURE (real system diagram) ----------', '// ---------- SLIDE 12 · APPENDIX: ARCHITECTURE (real system diagram) ----------')
s = s.replace('dark: true, pageNum: 11, pageTotal: TOTAL,', 'dark: true, pageNum: 12, pageTotal: TOTAL,')

open(path, "w", encoding="utf-8").write(s)
print("done")
