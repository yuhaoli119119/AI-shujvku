import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";

const execFileAsync = promisify(execFile);
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const systemRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(systemRoot, "..");
const defaultOutputDir = path.join(systemRoot, "deliverables", "impact_metadata_20260621");
const outputDir = process.env.LITAI_IMPACT_METADATA_OUTPUT_DIR
  ? path.resolve(process.env.LITAI_IMPACT_METADATA_OUTPUT_DIR)
  : defaultOutputDir;
const composeFile = path.join(systemRoot, "docker-compose.yml");
const composeArgs = ["compose", "--project-directory", systemRoot, "-f", composeFile, "exec", "-T", "postgres"];

function normalizeImportTarget(value) {
  if (value.startsWith("file://")) return value;
  if (value.includes(path.sep) || value.endsWith(".mjs")) return pathToFileURL(path.resolve(value)).href;
  return value;
}

async function pathExists(targetPath) {
  try {
    await fs.access(targetPath);
    return true;
  } catch {
    return false;
  }
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function artifactToolModulePath(nodeModulesDir) {
  return path.join(nodeModulesDir, "@oai", "artifact-tool", "dist", "artifact_tool.mjs");
}

function nodePathDirectories() {
  return (process.env.NODE_PATH || "")
    .split(path.delimiter)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

async function codexRuntimeNodeModuleDirs() {
  const runtimesRoot = path.join(os.homedir(), ".cache", "codex-runtimes");
  if (!(await pathExists(runtimesRoot))) return [];

  try {
    const entries = await fs.readdir(runtimesRoot, { withFileTypes: true });
    return entries
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(runtimesRoot, entry.name, "dependencies", "node", "node_modules"));
  } catch {
    return [];
  }
}

function currentRuntimeNodeModulesDir() {
  return path.resolve(path.dirname(process.execPath), "..", "node_modules");
}

async function loadArtifactTool() {
  const explicitOverride = process.env.LITAI_ARTIFACT_TOOL_MODULE?.trim();
  if (explicitOverride) {
    return import(normalizeImportTarget(explicitOverride));
  }

  try {
    return await import("@oai/artifact-tool");
  } catch (error) {
    if (error?.code !== "ERR_MODULE_NOT_FOUND") {
      throw error;
    }
  }

  const nodeModulesDirs = unique([
    currentRuntimeNodeModulesDir(),
    path.join(repoRoot, "node_modules"),
    path.join(systemRoot, "node_modules"),
    path.join(systemRoot, "frontend", "node_modules"),
    ...nodePathDirectories(),
    ...(await codexRuntimeNodeModuleDirs()),
  ]);

  for (const nodeModulesDir of nodeModulesDirs) {
    const candidate = artifactToolModulePath(nodeModulesDir);
    if (await pathExists(candidate)) {
      return import(pathToFileURL(candidate).href);
    }
  }

  throw new Error(
    [
      "Could not locate @oai/artifact-tool automatically.",
      "Set LITAI_ARTIFACT_TOOL_MODULE to an explicit module path if your environment is non-standard.",
      `Checked node_modules roots: ${nodeModulesDirs.join(", ") || "(none)"}`,
    ].join(" "),
  );
}

const { SpreadsheetFile, Workbook } = await loadArtifactTool();

const punctuationRegex = /[\s\.,;:!?'"]+|[()\[\]{}\-_/\\]+/g;

function normalizeJournalName(value) {
  if (!value) return "";
  return value.trim().toLowerCase().replace(punctuationRegex, " ").trim().replace(/\s+/g, " ");
}

function decodeHtmlEntities(text) {
  return text
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, "\"")
    .replace(/&#39;/gi, "'");
}

function abbreviates(base, short) {
  const a = base.replace(/[^a-z]/gi, "").toLowerCase();
  const b = short.replace(/[^a-z]/gi, "").toLowerCase();
  return Boolean(a && b) && a.startsWith(b);
}

function deriveLookupJournal(rawJournal) {
  const cleaned = decodeHtmlEntities(rawJournal).replace(/\s+/g, " ").trim();
  const tokens = cleaned.split(" ");
  if (tokens.length === 0) return cleaned;

  if (tokens.length % 2 === 0) {
    const half = tokens.length / 2;
    const first = tokens.slice(0, half).join(" ");
    const second = tokens.slice(half).join(" ");
    if (first.toLowerCase() === second.toLowerCase()) return first;
  }

  const dotIndex = tokens.findIndex((token) => token.includes("."));
  if (dotIndex > 1) {
    return tokens.slice(0, dotIndex).join(" ");
  }

  for (let suffixLen = Math.min(3, Math.floor(tokens.length / 2)); suffixLen >= 1; suffixLen -= 1) {
    const prefix = tokens.slice(0, suffixLen);
    const suffix = tokens.slice(tokens.length - suffixLen);
    const looksLikeAbbreviation = suffix.every((token, index) => abbreviates(prefix[index], token));
    const isShorter = suffix.some((token, index) => token.length < prefix[index].length);
    if (looksLikeAbbreviation && isShorter) {
      return prefix.join(" ");
    }
  }

  return cleaned;
}

function toCsv(rows) {
  return rows
    .map((row) =>
      row
        .map((value) => {
          const text = value == null ? "" : String(value);
          if (/[",\n]/.test(text)) return `"${text.replace(/"/g, "\"\"")}"`;
          return text;
        })
        .join(","),
    )
    .join("\n");
}

const missingJournalRecovery = [
  {
    title: "Aligned d-orbital energy level of dual-atom sites catalysts for oxygen reduction reaction in anion exchange membrane fuel cells",
    doi: "10.1038/s41467-025-63322-4",
    resolved_journal: "Nature Communications",
    confidence: "verified_external",
    evidence: "DOI/article page",
    source_url: "https://www.nature.com/articles/s41467-025-63322-4",
    note: "Missing journal in local DB; article page confirms Nature Communications.",
  },
  {
    title: "Synergistic effects of single atoms and nanoparticles: Emerging opportunities for electrocatalysis",
    doi: "10.26599/NR.2025.94907441",
    resolved_journal: "Nano Research",
    confidence: "verified_external",
    evidence: "DOI/article page",
    source_url: "https://www.sciopen.com/article/10.26599/NR.2025.94907441",
    note: "SciOpen result shows Nano Research, 2025.",
  },
  {
    title: "Raman and IR spectra of graphdiyne nanoribbons",
    doi: "10.1103/PhysRevMaterials.4.014001",
    resolved_journal: "Physical Review Materials",
    confidence: "verified_external",
    evidence: "DOI/article page",
    source_url: "https://link.aps.org/doi/10.1103/PhysRevMaterials.4.014001",
    note: "Resolved from DOI embedded in filename and APS landing page.",
  },
  {
    title: "Fundamentals of Electrochemical CO2 Reduction on Single-Metal-Atom Catalysts",
    doi: "10.1021/acscatal.0c02643",
    resolved_journal: "ACS Catalysis",
    confidence: "verified_external",
    evidence: "publisher DOI page",
    source_url: "https://pubs.acs.org/doi/10.1021/acscatal.0c02643",
    note: "Publisher DOI page confirms ACS Catalysis.",
  },
  {
    title: "High-throughput screening of heterogeneous transition metal dualatom catalysts by synergistic effect for nitrate reduction to ammonia",
    doi: "",
    resolved_journal: "Advanced Functional Materials",
    confidence: "verified_external",
    evidence: "publisher DOI page",
    source_url: "https://advanced.onlinelibrary.wiley.com/doi/abs/10.1002/adfm.202301493",
    note: "Title match on Wiley page shows Advanced Functional Materials.",
  },
  {
    title: "Carbon mono and dioxide hydrogenation over pure and metal oxide decorated graphene oxide substrates: insight from DFT",
    doi: "10.4236/graphene.2013.23016",
    resolved_journal: "Graphene",
    confidence: "verified_external",
    evidence: "journal article page",
    source_url: "https://www.scirp.org/journal/paperinformation?paperid=34767",
    note: "SCIRP article page shows journal Graphene.",
  },
  {
    title: "BORON-DOPED GRAPHENE AS ACTIVE ELECTROCATALYST FOR OXYGEN REDUCTION REACTION AT A FUEL-CELL CATHODE",
    doi: "10.1016/j.jcat.2014.07.024",
    resolved_journal: "Journal of Catalysis",
    confidence: "verified_external",
    evidence: "arXiv journal reference",
    source_url: "https://arxiv.org/abs/1607.08180",
    note: "arXiv record lists journal reference Journal of Catalysis 318 (2014) 203-210.",
  },
  {
    title: "[Warning] Failed to read PDF text with pypdf: Stream has ended unexpectedly",
    doi: "",
    resolved_journal: "",
    confidence: "unresolved",
    evidence: "local markdown unusable",
    source_url: "",
    note: "PDF and derived markdown both failed; needs manual PDF inspection or re-download.",
  },
  {
    title: "[Warning] Failed to read PDF text with pypdf: Stream has ended unexpectedly",
    doi: "",
    resolved_journal: "",
    confidence: "unresolved",
    evidence: "local markdown unusable",
    source_url: "",
    note: "PDF and derived markdown both failed; needs manual PDF inspection or re-download.",
  },
];

const supplementalMaterialsJournals = [
  ["Advanced Functional Materials", "materials/electrocatalysis", "missing-paper target; likely recurring venue", "model_knowledge_candidate"],
  ["Nano Energy", "energy materials", "high overlap with electrocatalysis and energy conversion", "model_knowledge_candidate"],
  ["Advanced Science", "broad materials", "common outlet for catalysis and materials mechanisms", "model_knowledge_candidate"],
  ["Small", "nanomaterials", "frequent nano electrocatalysis venue", "model_knowledge_candidate"],
  ["Small Methods", "methods/materials", "common for catalyst design workflows", "model_knowledge_candidate"],
  ["Materials Horizons", "materials chemistry", "high-relevance materials discovery venue", "model_knowledge_candidate"],
  ["ACS Materials Letters", "materials", "newer materials-focused ACS venue", "model_knowledge_candidate"],
  ["Materials Today Energy", "energy materials", "relevant to electrocatalysis and storage", "model_knowledge_candidate"],
  ["Materials Today Physics", "materials physics", "useful for theory-heavy materials papers", "model_knowledge_candidate"],
  ["Chemistry of Materials", "materials chemistry", "core materials venue absent from current library list", "model_knowledge_candidate"],
  ["Carbon", "carbon materials", "high relevance to graphene/graphdiyne corpus", "model_knowledge_candidate"],
  ["Journal of Physical Chemistry C", "surface/materials chemistry", "common DFT and catalysis venue", "model_knowledge_candidate"],
  ["Applied Catalysis B: Environment and Energy", "applied catalysis", "strong relevance for catalytic performance papers", "model_knowledge_candidate"],
  ["ACS Sustainable Chemistry & Engineering", "sustainable materials", "often overlaps with catalytic reduction topics", "model_knowledge_candidate"],
  ["Energy Storage Materials", "energy materials", "adjacent venue for electrochemical materials work", "model_knowledge_candidate"],
  ["2D Materials", "2D materials", "useful for graphene/graphdiyne-related studies", "model_knowledge_candidate"],
  ["Materials Today Nano", "nanomaterials", "adjacent nano/materials review venue", "model_knowledge_candidate"],
  ["InfoMat", "materials informatics", "fits computational materials discovery workflows", "model_knowledge_candidate"],
];

const impactByLookupJournal = {
  "Nature Communications": {
    impact_factor: 18.1,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=pB7xG5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Angewandte Chemie International Edition": {
    impact_factor: 17.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=grRvAr",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  arXiv: {
    impact_factor: "",
    impact_factor_year: "",
    impact_factor_source: "",
    source_url: "https://arxiv.org/",
    note: "Preprint server; no Journal Impact Factor.",
    verification_status: "not_applicable",
  },
  "Energy & Environmental Science": {
    impact_factor: 30.5,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=DbG97r",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Journal of Materials Chemistry A": {
    impact_factor: 9.2,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=pqOAPD",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Scientific Reports": {
    impact_factor: 4.9,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=rAXQeD",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "ACS Applied Materials & Interfaces": {
    impact_factor: 7.8,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=95EBBD",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "ACS Catalysis": {
    impact_factor: 13.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=DMLO65",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Advanced Energy Materials": {
    impact_factor: 25.5,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=r8M8xp",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "AIChE Journal": {
    impact_factor: 4.4,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=R52OqD",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Applied Surface Science": {
    impact_factor: 6.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=XDZOOr",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  Catalysts: {
    impact_factor: 4.5,
    impact_factor_year: 2025,
    impact_factor_source: "mdpi_2025",
    source_url: "https://www.mdpi.com/journal/catalysts/imprint",
    note: "Used publisher page because AbleSci search was not recoverable for this title in the current session.",
    verification_status: "filled_verified",
  },
  ChemCatChem: {
    impact_factor: 3.9,
    impact_factor_year: 2025,
    impact_factor_source: "ablesci_2025-06-18",
    source_url: "https://www.ablesci.com/journal/detail?id=W5O3Q5",
    note: "AbleSci page surfaced 2025 latest IF in search snippet.",
    verification_status: "filled_verified",
  },
  "Chemical Engineering Journal": {
    impact_factor: 12.5,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=w5g8JD",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Chemical Science": {
    impact_factor: 8.1,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/index?keywords=Chemical+Science&order=if_desc",
    note: "Chemical Science row verified from AbleSci search results page.",
    verification_status: "filled_verified",
  },
  "Electrochemical Energy Reviews": {
    impact_factor: 36.3,
    impact_factor_year: 2025,
    impact_factor_source: "ablesci_2025-06-18",
    source_url: "https://www.ablesci.com/journal/detail?id=peVyx5",
    note: "AbleSci page surfaced 2025 latest IF in search snippet.",
    verification_status: "filled_verified",
  },
  "Industrial & Engineering Chemistry Research": {
    impact_factor: 3.9,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=rRlZW5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Industrial Chemistry & Materials": {
    impact_factor: 15.7,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=rRzEAr",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Interdisciplinary materials": {
    impact_factor: "",
    impact_factor_year: "",
    impact_factor_source: "",
    source_url: "",
    note: "No reliable IF page recovered in current lookup; leave blank pending manual verification.",
    verification_status: "unresolved",
  },
  "Journal of Energy Chemistry": {
    impact_factor: 15.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=pPYMB5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Journal of the American Chemical Society": {
    impact_factor: 16.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=pnGnw5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  Molecules: {
    impact_factor: 5.1,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=DXvd1p",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Nano Research Energy": {
    impact_factor: "",
    impact_factor_year: "",
    impact_factor_source: "",
    source_url: "https://www.ablesci.com/journal/detail?id=DbxQVp",
    note: "AbleSci page shows no IF yet.",
    verification_status: "no_if_listed",
  },
  "Nano Select": {
    impact_factor: 3.5,
    impact_factor_year: 2025,
    impact_factor_source: "ablesci_2025-06-18",
    source_url: "https://www.ablesci.com/journal/detail?id=rzEmkD",
    note: "AbleSci page surfaced 2025 latest IF in search snippet.",
    verification_status: "filled_verified",
  },
  "npj Materials Sustainability": {
    impact_factor: "",
    impact_factor_year: "",
    impact_factor_source: "",
    source_url: "",
    note: "No reliable IF page recovered in current lookup; leave blank pending manual verification.",
    verification_status: "unresolved",
  },
  "npj Quantum Materials": {
    impact_factor: 6.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=5jNMV5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "npj Quantum Materials npj Quantum": {
    impact_factor: 6.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=5jNMV5",
    note: "Alias row mapped to npj Quantum Materials using the same verified IF.",
    verification_status: "filled_verified",
  },
  "Physical Chemistry Chemical Physics": {
    impact_factor: 3.0,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=52a68p",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Topics in Catalysis": {
    impact_factor: 3.0,
    impact_factor_year: 2025,
    impact_factor_source: "ablesci_2025-06-18",
    source_url: "https://www.ablesci.com/journal/detail?id=5Nbyb5",
    note: "AbleSci page surfaced 2025 latest IF in search snippet.",
    verification_status: "filled_verified",
  },
};

const recoveryImpactByJournal = {
  "Nature Communications": impactByLookupJournal["Nature Communications"],
  "Nano Research": {
    impact_factor: 9.4,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=pLMGG5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "Physical Review Materials": {
    impact_factor: 3.6,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=r7mnR5",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  "ACS Catalysis": impactByLookupJournal["ACS Catalysis"],
  "Advanced Functional Materials": {
    impact_factor: 19.9,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=GDGWAD",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
  Graphene: {
    impact_factor: "",
    impact_factor_year: "",
    impact_factor_source: "",
    source_url: "",
    note: "Journal recovered, but no stable IF source was verified in this pass.",
    verification_status: "unresolved_if",
  },
  "Journal of Catalysis": {
    impact_factor: 6.0,
    impact_factor_year: 2026,
    impact_factor_source: "ablesci_2026-06-17",
    source_url: "https://www.ablesci.com/journal/detail?id=DlNm0p",
    note: "AbleSci latest IF page, published 2026-06-17.",
    verification_status: "filled_verified",
  },
};

async function runSqlJson(sql) {
  const args = [...composeArgs, "psql", "-U", "literature_ai", "-d", "literature_ai", "-t", "-A", "-c", sql];
  const { stdout, stderr } = await execFileAsync("docker", args, { cwd: systemRoot, maxBuffer: 8 * 1024 * 1024 });
  if (stderr && stderr.trim()) {
    console.error(stderr);
  }
  return JSON.parse(stdout.trim() || "[]");
}

async function main() {
  await fs.mkdir(outputDir, { recursive: true });

  const journalSql = `
    SELECT COALESCE(json_agg(x ORDER BY x.paper_count DESC, x.journal ASC), '[]'::json)
    FROM (
      SELECT
        btrim(journal) AS journal,
        COUNT(*)::int AS paper_count
      FROM papers
      WHERE journal IS NOT NULL AND btrim(journal) <> ''
      GROUP BY 1
    ) x;
  `;
  const missingSql = `
    SELECT COALESCE(json_agg(x ORDER BY x.title ASC), '[]'::json)
    FROM (
      SELECT
        id::text AS paper_id,
        title
      FROM papers
      WHERE journal IS NULL OR btrim(journal) = ''
    ) x;
  `;

  const journalRows = await runSqlJson(journalSql);
  const missingRows = await runSqlJson(missingSql);

  const importRows = journalRows.map((row) => {
    const lookupJournal = deriveLookupJournal(row.journal);
    const htmlDecoded = decodeHtmlEntities(row.journal);
    const normalized = normalizeJournalName(row.journal);
    const normalizedLookup = normalizeJournalName(lookupJournal);
    let matchStatus = "exact_string";
    if (htmlDecoded !== row.journal) matchStatus = "html_entity_variant";
    if (lookupJournal !== htmlDecoded) matchStatus = "alias_or_suffix_variant";
    const impact = impactByLookupJournal[lookupJournal] ?? null;
    return {
      journal: row.journal,
      impact_factor: impact?.impact_factor ?? "",
      impact_factor_year: impact?.impact_factor_year ?? "",
      impact_factor_source: impact?.impact_factor_source ?? "",
      issn: "",
      eissn: "",
      note: impact?.note ?? "",
      paper_count: row.paper_count,
      lookup_journal: lookupJournal,
      normalized_journal: normalized,
      normalized_lookup_journal: normalizedLookup,
      match_status: matchStatus,
      source_url: impact?.source_url ?? "",
      verification_status: impact?.verification_status ?? "unresolved",
    };
  });

  const lookupGroups = new Map();
  for (const row of importRows) {
    const key = row.lookup_journal;
    const current = lookupGroups.get(key) ?? {
      lookup_journal: key,
      import_row_count: 0,
      total_papers: 0,
      import_journals: [],
      match_statuses: new Set(),
    };
    current.import_row_count += 1;
    current.total_papers += Number(row.paper_count);
    current.import_journals.push(row.journal);
    current.match_statuses.add(row.match_status);
    lookupGroups.set(key, current);
  }

  const groupRows = [...lookupGroups.values()]
    .sort((a, b) => b.total_papers - a.total_papers || a.lookup_journal.localeCompare(b.lookup_journal))
    .map((group) => ({
      lookup_journal: group.lookup_journal,
      import_row_count: group.import_row_count,
      total_papers: group.total_papers,
      import_journals: group.import_journals.join(" | "),
      copy_hint: group.import_row_count > 1 ? "Same IF should be copied to all listed import_journals." : "",
      match_statuses: [...group.match_statuses].join(" | "),
    }));

  const workbook = Workbook.create();

  const readme = workbook.worksheets.add("README");
  readme.showGridLines = false;
  readme.getRange("A1:B8").values = [
    ["Literature AI Impact Metadata Template", ""],
    ["What this file is", "A fillable template aligned to /api/library/impact-metadata/import."],
    ["How to use", "1. Review prefilled IF/year/source rows. 2. Fill remaining blanks or adjust disputed rows. 3. Rows sharing the same lookup_journal can reuse the same IF. 4. Export Import Template to CSV or keep extra columns; backend ignores columns outside the core import fields."],
    ["Core import fields", "journal, impact_factor, impact_factor_year, impact_factor_source, issn, eissn, note"],
    ["Current library coverage", `${journalRows.length} journal strings, ${missingRows.length} papers missing journal.`],
    ["Important limitation", "Current backend matching is exact after safe normalization only. Alias/suffix variants need separate import rows unless matching logic is enhanced later."],
    ["Suggested source discipline", "This workbook currently mixes 2026 and 2025 labels only where the live source page exposed different latest years. Normalize before final import if you need a single year."],
    ["Generated on", new Date().toISOString()],
  ];
  readme.getRange("A1:A8").format = { font: { bold: true, color: "#FFFFFF" }, fill: "#1F4E78" };
  readme.getRange("A1:B8").format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
  readme.getRange("A1:B8").format.wrapText = true;
  readme.getRange("A1:B8").format.autofitColumns();
  readme.getRange("A3:B3").format.rowHeight = 48;

  const importSheet = workbook.worksheets.add("Import Template");
  const importHeaders = [
    "journal",
    "impact_factor",
    "impact_factor_year",
    "impact_factor_source",
    "issn",
    "eissn",
    "note",
    "paper_count",
    "lookup_journal",
    "normalized_journal",
    "normalized_lookup_journal",
    "match_status",
    "source_url",
    "verification_status",
  ];
  importSheet.getRange(`A1:N${importRows.length + 1}`).values = [
    importHeaders,
    ...importRows.map((row) => importHeaders.map((header) => row[header] ?? "")),
  ];
  importSheet.getRange(`A1:N1`).format = {
    fill: "#0F766E",
    font: { bold: true, color: "#FFFFFF" },
  };
  importSheet.getRange(`A1:N${importRows.length + 1}`).format.borders = {
    preset: "all",
    style: "thin",
    color: "#D9E2F3",
  };
  importSheet.getRange(`B2:B${importRows.length + 1}`).format.numberFormat = "0.0";
  importSheet.getRange(`C2:C${importRows.length + 1}`).format.numberFormat = "0";
  importSheet.getRange(`H2:H${importRows.length + 1}`).format.numberFormat = "0";
  importSheet.getRange(`A1:N${importRows.length + 1}`).format.wrapText = true;
  importSheet.getRange(`A1:N${importRows.length + 1}`).format.autofitColumns();
  importSheet.freezePanes.freezeRows(1);
  importSheet.getRange(`L2:L${importRows.length + 1}`).conditionalFormats.add("containsText", {
    text: "alias_or_suffix_variant",
    format: { fill: "#FFF2CC", font: { color: "#7F6000" } },
  });
  importSheet.getRange(`L2:L${importRows.length + 1}`).conditionalFormats.add("containsText", {
    text: "html_entity_variant",
    format: { fill: "#FCE4D6", font: { color: "#833C0C" } },
  });
  importSheet.getRange(`N2:N${importRows.length + 1}`).conditionalFormats.add("containsText", {
    text: "filled_verified",
    format: { fill: "#E2F0D9", font: { color: "#375623" } },
  });
  importSheet.getRange(`N2:N${importRows.length + 1}`).conditionalFormats.add("containsText", {
    text: "unresolved",
    format: { fill: "#FEE2E2", font: { color: "#991B1B" } },
  });
  importSheet.getRange(`N2:N${importRows.length + 1}`).conditionalFormats.add("containsText", {
    text: "no_if_listed",
    format: { fill: "#EDEDED", font: { color: "#555555" } },
  });
  importSheet.getRange(`N2:N${importRows.length + 1}`).conditionalFormats.add("containsText", {
    text: "not_applicable",
    format: { fill: "#EDEDED", font: { color: "#555555" } },
  });

  const groupsSheet = workbook.worksheets.add("Lookup Groups");
  const groupHeaders = ["lookup_journal", "import_row_count", "total_papers", "import_journals", "copy_hint", "match_statuses"];
  groupsSheet.getRange(`A1:F${groupRows.length + 1}`).values = [
    groupHeaders,
    ...groupRows.map((row) => groupHeaders.map((header) => row[header] ?? "")),
  ];
  groupsSheet.getRange("A1:F1").format = {
    fill: "#7C3AED",
    font: { bold: true, color: "#FFFFFF" },
  };
  groupsSheet.getRange(`A1:F${groupRows.length + 1}`).format.borders = {
    preset: "all",
    style: "thin",
    color: "#E9D5FF",
  };
  groupsSheet.getRange(`A1:F${groupRows.length + 1}`).format.wrapText = true;
  groupsSheet.getRange(`A1:F${groupRows.length + 1}`).format.autofitColumns();
  groupsSheet.freezePanes.freezeRows(1);

  const missingSheet = workbook.worksheets.add("Missing Journals");
  const missingHeaders = ["paper_id", "title", "action_needed"];
  missingSheet.getRange(`A1:C${missingRows.length + 1}`).values = [
    missingHeaders,
    ...missingRows.map((row) => [row.paper_id, row.title, "Journal missing in source data; IF cannot be imported until journal is backfilled."]),
  ];
  missingSheet.getRange("A1:C1").format = {
    fill: "#B91C1C",
    font: { bold: true, color: "#FFFFFF" },
  };
  missingSheet.getRange(`A1:C${missingRows.length + 1}`).format.borders = {
    preset: "all",
    style: "thin",
    color: "#FECACA",
  };
  missingSheet.getRange(`A1:C${missingRows.length + 1}`).format.wrapText = true;
  missingSheet.getRange(`A1:C${missingRows.length + 1}`).format.autofitColumns();
  missingSheet.freezePanes.freezeRows(1);

  const recoverySheet = workbook.worksheets.add("Journal Recovery");
  const recoveryHeaders = ["title", "doi", "resolved_journal", "confidence", "evidence", "source_url", "impact_factor", "impact_factor_year", "impact_factor_source", "if_source_url", "note"];
  recoverySheet.getRange(`A1:K${missingJournalRecovery.length + 1}`).values = [
    recoveryHeaders,
    ...missingJournalRecovery.map((row) => {
      const impact = recoveryImpactByJournal[row.resolved_journal] ?? {};
      return recoveryHeaders.map((header) => {
        if (header === "if_source_url") return impact.source_url ?? "";
        if (header === "impact_factor" || header === "impact_factor_year" || header === "impact_factor_source") {
          return impact[header] ?? "";
        }
        return row[header] ?? "";
      });
    }),
  ];
  recoverySheet.getRange("A1:K1").format = {
    fill: "#1D4ED8",
    font: { bold: true, color: "#FFFFFF" },
  };
  recoverySheet.getRange(`A1:K${missingJournalRecovery.length + 1}`).format.borders = {
    preset: "all",
    style: "thin",
    color: "#BFDBFE",
  };
  recoverySheet.getRange(`G2:G${missingJournalRecovery.length + 1}`).format.numberFormat = "0.0";
  recoverySheet.getRange(`H2:H${missingJournalRecovery.length + 1}`).format.numberFormat = "0";
  recoverySheet.getRange(`A1:K${missingJournalRecovery.length + 1}`).format.wrapText = true;
  recoverySheet.getRange(`A1:K${missingJournalRecovery.length + 1}`).format.autofitColumns();
  recoverySheet.freezePanes.freezeRows(1);
  recoverySheet.getRange(`D2:D${missingJournalRecovery.length + 1}`).conditionalFormats.add("containsText", {
    text: "unresolved",
    format: { fill: "#FEE2E2", font: { color: "#991B1B" } },
  });

  const supplementSheet = workbook.worksheets.add("Supplemental Journals");
  const existingLookupSet = new Set(groupRows.map((row) => row.lookup_journal.toLowerCase()));
  const supplementHeaders = ["journal", "domain_focus", "why_add", "candidate_source", "already_in_current_db"];
  const supplementRows = supplementalMaterialsJournals
    .map(([journal, domain_focus, why_add, candidate_source]) => ({
      journal,
      domain_focus,
      why_add,
      candidate_source,
      already_in_current_db: existingLookupSet.has(journal.toLowerCase()) ? "yes" : "no",
    }))
    .filter((row) => row.already_in_current_db === "no");
  supplementSheet.getRange(`A1:E${supplementRows.length + 1}`).values = [
    supplementHeaders,
    ...supplementRows.map((row) => supplementHeaders.map((header) => row[header] ?? "")),
  ];
  supplementSheet.getRange("A1:E1").format = {
    fill: "#7C2D12",
    font: { bold: true, color: "#FFFFFF" },
  };
  supplementSheet.getRange(`A1:E${supplementRows.length + 1}`).format.borders = {
    preset: "all",
    style: "thin",
    color: "#FED7AA",
  };
  supplementSheet.getRange(`A1:E${supplementRows.length + 1}`).format.wrapText = true;
  supplementSheet.getRange(`A1:E${supplementRows.length + 1}`).format.autofitColumns();
  supplementSheet.freezePanes.freezeRows(1);

  const csvHeaders = importHeaders;
  const csvRows = [
    csvHeaders,
    ...importRows.map((row) => csvHeaders.map((header) => row[header] ?? "")),
  ];
  await fs.writeFile(path.join(outputDir, "impact_metadata_import_template.csv"), `${toCsv(csvRows)}\n`, "utf8");
  await fs.writeFile(
    path.join(outputDir, "impact_metadata_lookup_groups.json"),
    `${JSON.stringify(groupRows, null, 2)}\n`,
    "utf8",
  );
  await fs.writeFile(
    path.join(outputDir, "missing_journal_recovery.json"),
    `${JSON.stringify(missingJournalRecovery, null, 2)}\n`,
    "utf8",
  );
  await fs.writeFile(
    path.join(outputDir, "supplemental_materials_journals.json"),
    `${JSON.stringify(supplementRows, null, 2)}\n`,
    "utf8",
  );

  const preview = await workbook.render({ sheetName: "Import Template", range: "A1:N20", scale: 1.5, format: "png" });
  await fs.writeFile(path.join(outputDir, "import_template_preview.png"), new Uint8Array(await preview.arrayBuffer()));

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(path.join(outputDir, "literature_ai_impact_metadata_template.xlsx"));

  console.log(
    JSON.stringify({
      outputDir,
      importRowCount: importRows.length,
      lookupGroupCount: groupRows.length,
      missingJournalPaperCount: missingRows.length,
    }),
  );
}

try {
  await main();
  // `@oai/artifact-tool` can leave the process with a non-zero native exit on
  // Windows even after all outputs are successfully written. Exit explicitly so
  // CLI callers get a stable success code.
  process.exit(0);
} catch (error) {
  console.error(error);
  process.exit(1);
}
