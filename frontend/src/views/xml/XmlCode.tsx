import { useEffect, useRef } from "react";
import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { xml } from "@codemirror/lang-xml";
import { HighlightStyle, syntaxHighlighting, foldGutter, foldKeymap, bracketMatching, indentOnInput } from "@codemirror/language";
import { lintGutter, setDiagnostics, Diagnostic as CmDiagnostic } from "@codemirror/lint";
import { tags as t } from "@lezer/highlight";
import type { Json } from "../../api";

/* The XML source: syntax colours for structure (tags, attributes, values, comments) and the check's own
   colours for problems — red for errors, amber for warnings — on the line, in the gutter and on hover. */

const syntax = HighlightStyle.define([
  { tag: [t.tagName, t.angleBracket], color: "#8fbbea" },
  { tag: t.attributeName, color: "#b9c6d3", fontStyle: "italic" },
  { tag: t.attributeValue, color: "#d8b0f0" },
  { tag: t.string, color: "#d8b0f0" },
  { tag: t.comment, color: "#7f8d9b", fontStyle: "italic" },
  { tag: [t.processingInstruction, t.documentMeta], color: "#8796a5" },
  { tag: t.character, color: "#e3b505" },
  { tag: t.content, color: "#e9eef2" },
  { tag: t.invalid, color: "#ff8a80", textDecoration: "underline wavy" },
]);

const theme = EditorView.theme({
  "&": { backgroundColor: "#1b232d", color: "#e9eef2", fontSize: "12.5px", height: "100%" },
  ".cm-scroller": { fontFamily: "ui-monospace, 'Cascadia Mono', 'Consolas', monospace", lineHeight: "1.55" },
  ".cm-content": { caretColor: "#e3b505" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "#e3b505" },
  ".cm-gutters": { backgroundColor: "#161d26", color: "#5f6f80", border: "none" },
  ".cm-activeLine": { backgroundColor: "rgba(255,255,255,0.035)" },
  ".cm-activeLineGutter": { backgroundColor: "rgba(255,255,255,0.05)", color: "#b9c6d3" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": { backgroundColor: "rgba(143,187,234,0.28) !important" },
  ".cm-selectionMatch": { backgroundColor: "rgba(227,181,5,0.16)" },
  ".cm-matchingBracket": { backgroundColor: "rgba(143,187,234,0.25)", outline: "none" },
  /* problems: ASTHRA's red / amber */
  ".cm-lintRange-error": { backgroundImage: "none", textDecoration: "underline wavy #ff6b5e", textUnderlineOffset: "3px" },
  ".cm-lintRange-warning": { backgroundImage: "none", textDecoration: "underline wavy #f5b25a", textUnderlineOffset: "3px" },
  ".cm-lintRange-info": { backgroundImage: "none", textDecoration: "underline dotted #8fbbea" },
  ".cm-gutter-lint": { width: "14px" },
  ".cm-tooltip": { backgroundColor: "#24303c", border: "1px solid #3a4a5a", color: "#e9eef2" },
  ".cm-diagnostic": { padding: "4px 8px", fontFamily: "var(--font)", fontSize: "12.5px" },
  ".cm-diagnostic-error": { borderLeft: "4px solid #ff6b5e" },
  ".cm-diagnostic-warning": { borderLeft: "4px solid #f5b25a" },
  ".cm-foldGutter span": { color: "#5f6f80" },
}, { dark: true });

export function toCm(doc: EditorState["doc"], diags: Json[]): CmDiagnostic[] {
  const out: CmDiagnostic[] = [];
  for (const d of diags) {
    if (!d.line || d.line < 1 || d.line > doc.lines) continue;
    const ln = doc.line(d.line);
    const lead = ln.text.length - ln.text.trimStart().length;
    let from = ln.from + lead, to = ln.to;
    if (d.column && d.column > 0) from = Math.min(ln.to, ln.from + d.column - 1);
    if (to <= from) to = Math.min(doc.length, from + 1);
    const sev = d.severity === "fatal" || d.severity === "error" ? "error" : d.severity === "warning" ? "warning" : "info";
    out.push({ from, to, severity: sev, source: d.rule_id, message: d.message + (d.suggestion ? `\n${d.suggestion}` : "") });
  }
  return out;
}

export function XmlCode({ text, onChange, readOnly, diagnostics, gotoLine }: {
  text: string; onChange: (t: string) => void; readOnly: boolean; diagnostics: Json[]; gotoLine?: { line: number; n: number } | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const ro = useRef(new Compartment());
  const cb = useRef(onChange);
  cb.current = onChange;

  useEffect(() => {
    const v = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: text,
        extensions: [
          lineNumbers(), foldGutter(), lintGutter(), highlightActiveLineGutter(), history(), drawSelection(),
          indentOnInput(), bracketMatching(), highlightActiveLine(), highlightSelectionMatches(),
          xml(), syntaxHighlighting(syntax), theme, EditorView.lineWrapping,
          keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, ...foldKeymap, indentWithTab]),
          ro.current.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
          EditorView.updateListener.of((u) => { if (u.docChanged) cb.current(u.state.doc.toString()); }),
        ],
      }),
    });
    view.current = v;
    return () => { v.destroy(); view.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // outside changes (document view, revert, save) replace the text without losing the scroll position
  useEffect(() => {
    const v = view.current;
    if (v && v.state.doc.toString() !== text) v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: text } });
  }, [text]);

  useEffect(() => {
    view.current?.dispatch({ effects: ro.current.reconfigure([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]) });
  }, [readOnly]);

  useEffect(() => {
    const v = view.current;
    if (v) v.dispatch(setDiagnostics(v.state, toCm(v.state.doc, diagnostics)));
  }, [diagnostics, text]);

  useEffect(() => {
    const v = view.current;
    if (!v || !gotoLine || gotoLine.line > v.state.doc.lines) return;
    const ln = v.state.doc.line(gotoLine.line);
    v.dispatch({ selection: { anchor: ln.from }, effects: EditorView.scrollIntoView(ln.from, { y: "center" }) });
    v.focus();
  }, [gotoLine]);

  return <div className="xml-code" ref={host} />;
}
