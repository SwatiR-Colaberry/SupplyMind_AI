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

  cached = { plan, progress, manifest, profile, stories };
  return cached;
}

export function releaseForStory(plan, storyId) {
  return (plan?.releases || []).find((r) => (r.story_ids || []).includes(storyId));
}

export function requirementsFulfilledBy(plan, storyId) {
  return (plan?.requirements || []).filter((r) => (r.fulfilled_by || []).includes(storyId));
}
