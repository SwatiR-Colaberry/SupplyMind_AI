// Loads .colaberry/*.json at runtime and joins plan + progress on story id.
// Nothing here is a copy of the data — every value is re-read on each load.

const DATA_PATHS = {
  plan: "./.colaberry/plan.json",
  progress: "./.colaberry/progress.json",
  manifest: "./.colaberry/manifest.json",
  profile: "./.colaberry/profile.json",
};

async function fetchJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to load ${path}: ${res.status}`);
  return res.json();
}

let cached = null;

export async function loadData() {
  if (cached) return cached;

  const [plan, progress, manifest, profile] = await Promise.all([
    fetchJson(DATA_PATHS.plan).catch(() => null),
    fetchJson(DATA_PATHS.progress).catch(() => null),
    fetchJson(DATA_PATHS.manifest).catch(() => null),
    fetchJson(DATA_PATHS.profile).catch(() => null),
  ]);

  const progressById = new Map();
  (progress?.stories || []).forEach((s) => progressById.set(s.id, s));

  const stories = (plan?.stories || []).map((story) => {
    const p = progressById.get(story.id);
    return {
      ...story,
      verification: p?.verification || { state: "not_started", commit: null, points: 0 },
      criteria: p?.criteria || [],
    };
  });

  // STORY-000 (this Command Center) is pre-work tracked in progress.json but is not
  // part of the delivery plan, so it never appears in plan.stories. Surface it from
  // progress.json alone so PM views don't silently drop the story being demoed.
  const p000 = progressById.get("STORY-000");
  if (p000 && !stories.some((s) => s.id === "STORY-000")) {
    stories.unshift({
      id: "STORY-000",
      title: "Command Center",
      release: null,
      narrative: "As the project owner, I want a single page that shows what SupplyMind AI is, what it's meant to move, and how far along it is, so that I have one place to demo from and one place that keeps everyone honest about what's actually built.",
      due_on: null,
      verification: p000.verification,
      criteria: p000.criteria,
    });
  }

  cached = { plan, progress, manifest, profile, stories };
  return cached;
}

export function releaseForStory(plan, storyId) {
  return (plan?.releases || []).find((r) => (r.story_ids || []).includes(storyId));
}

export function requirementsFulfilledBy(plan, storyId) {
  return (plan?.requirements || []).filter((r) => (r.fulfilled_by || []).includes(storyId));
}

export function storyById(stories, storyId) {
  return (stories || []).find((s) => s.id === storyId) || null;
}

export function personaFromNarrative(narrative) {
  const m = String(narrative || "").match(/^As an? ([^,]+),/i);
  return m ? m[1].trim() : null;
}
