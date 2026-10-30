const PROVIDER_ACCESS_ERROR = /HTTP 403.*(?:api|openapi)\.tripo3d\.ai.*error code:\s*1010/i;
const COPYRIGHT_ERROR = /\b(copyright(?:ed)?|trademark|intellectual property|ip infringement|protected (?:content|work)|third party content)\b/i;
const REFERENCE_ERROR = /\b(reference photo|reference image|input image|source image)\b/i;
const SAFETY_ERROR = /\b(safety|moderation|content (?:filter|policy)|policy violation|responsible ai|recitation|prohibited|unsafe|nsfw|nudity|abusive|blocked|rejected|flagged|disallowed)\b/i;
const TEMPORARY_ERROR = /\b(temporar(?:y|ily)|timed? out|timeout(?: error)?|etimedout|econnreset|rate limit|too many requests|server error|unavailable|network|connection|http 429|http 5\d\d)\b/i;

function jobError(job) {
  return String(job?.error || "").trim().replace(/[_-]+/g, " ");
}

function fighterName(job) {
  return String(job?.character?.name || job?.name || "your fighter").trim();
}

export function blockedArtworkDetails(job, stage = job?.stage) {
  const stages = { tpose: "Model sheet", portrait: "Portrait", stock: "Stock icon", emblem: "Emblem" };
  const progressStage = { 18: "tpose", 75: "portrait", 81: "stock", 86: "emblem" }[job?.progress];
  const subject = stages[stage] || stages[progressStage] || "Artwork";
  return {
    stageLabel: `${subject} blocked`,
    error: `The image provider blocked the ${subject.toLowerCase()} but didn’t specify why.`,
    retryLabel: "Retry generation",
  };
}

export function formatFighterJobCellError(job) {
  const error = jobError(job);
  if (PROVIDER_ACCESS_ERROR.test(error)) return "Provider unavailable";
  if (COPYRIGHT_ERROR.test(error)) return "Copyright blocked";
  if (REFERENCE_ERROR.test(error) && SAFETY_ERROR.test(error)) return "Photo rejected";
  if (SAFETY_ERROR.test(error)) return blockedArtworkDetails(job).stageLabel;
  return "Generation failed";
}

export function formatFighterJobError(job) {
  const error = jobError(job);
  const name = fighterName(job);

  if (PROVIDER_ACCESS_ERROR.test(error)) {
    return `We couldn’t generate ${name} because the 3D provider is blocking our connection. Please try again later; you don’t need to change your photo or fighter details.`;
  }
  if (COPYRIGHT_ERROR.test(error)) {
    return `We couldn’t create ${name} because the image provider flagged the artwork as copyrighted or protected content. Try a different photo or a design you have permission to use.`;
  }
  if (REFERENCE_ERROR.test(error) && SAFETY_ERROR.test(error)) {
    return `We couldn’t use the reference photo for ${name} because the image provider rejected it. Try again with a clear, standard photo that you have permission to use.`;
  }
  if (SAFETY_ERROR.test(error)) {
    return `We couldn’t create ${name}. ${blockedArtworkDetails(job).error} Try a different photo or adjust the fighter details.`;
  }
  if (TEMPORARY_ERROR.test(error)) {
    return `Generation for ${name} ran into a temporary provider issue. Please try again in a moment.`;
  }
  return `We couldn’t finish generating ${name}. Please try again with a different photo or fighter details.`;
}

// Failed jobs that were already present when this page loaded stay hidden.
// A job that was visible while it ran remains visible if it fails, letting the
// player inspect the failure until their next full page refresh.
export function reconcileVisibleFighterJobs(currentJobs = [], incomingJobs = []) {
  const tracked = new Map(currentJobs.map((job) => [job.id, job]));
  return incomingJobs
    .filter((job) => job?.id && (job.status !== "failed" || tracked.has(job.id)))
    .map((job) => {
      const existing = tracked.get(job.id);
      return existing && (existing.revision || 0) > (job.revision || 0) ? existing : job;
    })
    .sort((left, right) => String(right.createdAt || "").localeCompare(String(left.createdAt || "")));
}
