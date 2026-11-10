// Live updates for the chronicle. Text only: everything is written with textContent.
(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const setText = (selector, value) => {
    const node = $(selector);
    if (node && value !== null && value !== undefined) node.textContent = String(value);
  };
  const initialChapters = Number(($("[data-chapter-count]") || {}).textContent || NaN);
  const openVote = $("[data-open-vote]");

  const relative = (unixSeconds) => {
    const minutes = Math.round((unixSeconds * 1000 - Date.now()) / 60000);
    if (minutes > 1) return `in about ${minutes} minutes`;
    if (minutes >= -1) return "any minute now";
    return `${-minutes} minutes ago`;
  };

  const raidStatus = {
    active: "still abroad",
    defeated: "worn down, it has slunk away",
    retreated: "wandered off on its own",
  };

  const render = (state) => {
    const saga = state.saga || {};
    const settled = $("[data-saga-settled]");
    if (settled) {
      settled.textContent = saga.settledThroughRound
        ? `Certified through round ${saga.settledThroughRound}, ${saga.cumulativeWork} work units all time.`
        : "Not known right now. Unknown is not zero.";
    }
    setText("[data-saga-message]", saga.message);

    const next = $("[data-next-chapter]");
    if (next && state.nextChapterRoundEndsAt) {
      next.textContent = `The next hour's last round ends ${relative(state.nextChapterRoundEndsAt)}; `
        + "the chapter follows once that round is settled.";
    }

    const live = state.wheelLive;
    const liveNode = $("[data-wheel-live]");
    if (liveNode && live) {
      if (!live.known) {
        liveNode.textContent = `Unknown (${live.reason || "unavailable"}). Unknown is not zero.`;
      } else {
        const rpm = live.rpmMilli === null ? "?" : (live.rpmMilli / 1000).toFixed(1);
        const pending = live.openRoundWorkUnits === null ? "" : ` · ${live.openRoundWorkUnits} work units in the open round`;
        liveNode.textContent = `${live.coreStatus || "?"} · ${rpm} rpm · reading ${live.freshness || "?"}${pending}`;
      }
    }

    const raid = state.raid;
    if (raid && $("[data-raid]")) {
      setText("[data-raid-name]", raid.bossName);
      setText("[data-raid-damage]", raid.damage);
      setText("[data-raid-hp]", raid.hp);
      setText("[data-raid-certified]", raid.roundsCertified);
      setText("[data-raid-unknown]", raid.roundsUnknown);
      setText("[data-raid-status]", raidStatus[raid.status] || raid.status);
      const fill = $("[data-raid-fill]");
      if (fill) fill.value = raid.percent;
    }

    if (openVote && state.votes && String(state.votes.chapter) === openVote.dataset.openVote) {
      state.votes.counts.forEach((count, index) => {
        const node = openVote.querySelector(`[data-vote-count="${index}"]`);
        if (node) node.textContent = String(count);
      });
    }

    if (Number.isFinite(initialChapters) && state.chapters > initialChapters && !$(".new-chapter")) {
      const banner = document.createElement("div");
      banner.className = "new-chapter";
      const link = document.createElement("a");
      link.href = `/chapter/${state.chapters}`;
      link.textContent = `Chapter ${state.chapters} has just been written. Read it →`;
      banner.append(link);
      document.body.prepend(banner);
    }
  };

  const eta = $("[data-next-chapter][data-eta]");
  if (eta) {
    eta.textContent = `The next hour's last round ends ${relative(Number(eta.dataset.eta))}; `
      + "the chapter follows once that round is settled.";
  }

  const needsLive = $("[data-wheel]") || $("[data-raid]") || openVote;
  if (!needsLive) return;
  const poll = async () => {
    try {
      const response = await fetch("/api/state", { headers: { Accept: "application/json" } });
      if (response.ok) render(await response.json());
    } catch (error) {
      /* offline or restarting: keep what is on screen */
    }
  };
  poll();
  setInterval(() => { if (!document.hidden) poll(); }, 15000);
})();
