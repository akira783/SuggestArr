import { expect, test } from "@playwright/test";

// Deliberately unsigned fixture token. Every API request is intercepted below.
const token = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIiwidXNlcm5hbWUiOiJhZG1pbiIsInJvbGUiOiJhZG1pbiJ9.";

const cards = [
  {
    id: 438631, media_type: "movie", title: "Dune", release_date: "2021-09-15", year: 2021,
    rating: 7.8, overview: "A noble family becomes embroiled in a war for Arrakis.",
    rationale: "You loved Blade Runner 2049.", pick_type: "safe", poster_path: null,
    backdrop_path: null, streaming: null, ratings: null,
    trailer: { key: "n9xhJrPXop4", name: "Official Trailer" },
  },
  {
    id: 70523, media_type: "tv", title: "Dark", first_air_date: "2017-12-01", year: 2017,
    rating: 8.4, overview: "A missing child sets four families on a frantic hunt.",
    rationale: "A wildcard: German time-travel mystery.", pick_type: "explore", poster_path: null,
    backdrop_path: null, streaming: null, ratings: null, trailer: null,
  },
  {
    id: 1399, media_type: "tv", title: "Game of Thrones", year: 2011, rating: 8.5,
    overview: "Nine noble families fight for control.", rationale: "Epic fantasy.",
    pick_type: "safe", poster_path: null, backdrop_path: null, streaming: null, ratings: null, trailer: null,
  },
];

async function mockApi(page, { llmConfigured = true } = {}) {
  const calls = { batch: 0, votes: [], requests: [] };

  await page.addInitScript(() => {
    localStorage.setItem("suggestarr_tour_done", "1");
    localStorage.setItem("suggestarr_swipe_skip_calibration", "1");
  });

  await page.route("http://localhost:5000/**", async (route) => {
    const request = route.request();
    const { pathname, searchParams } = new URL(request.url());
    const json = (body, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      headers: {
        "access-control-allow-origin": "http://127.0.0.1:5173",
        "access-control-allow-credentials": "true",
      },
      body: JSON.stringify(body),
    });

    if (pathname === "/api/auth/status") return json({ auth_setup_complete: true, app_setup_complete: true });
    if (pathname === "/api/auth/login") return json({ access_token: token });
    if (pathname === "/api/auth/refresh") return json({ access_token: token });
    if (pathname === "/api/auth/me") return json({ id: 1, username: "admin", role: "admin" });
    if (pathname === "/api/config/fetch") return json({ AUTH_MODE: "enabled" });
    if (pathname === "/api/config/status") return json({ setup_completed: true, is_complete: true });
    if (pathname === "/api/automation/requests") return json({ data: [], total_pages: 1, total_sources: 0, total_requests: 0, request_users: [] });

    if (pathname === "/api/swipe/status") {
      return json({
        status: "success", account: true, llm_configured: llmConfigured, media_history: true,
        streaming_region: null, ratings_enabled: false, votes: 20, profile_ready: true,
        calibration: { done: 15, target: 15, complete: true },
      });
    }
    if (pathname === "/api/swipe/batch") {
      calls.batch += 1;
      calls.lastNovelty = searchParams.get("novelty");
      // The first call only starts the generation, as the real server does.
      const pending = calls.batch === 1;
      return json({
        status: "success", mode: "normal", pending, cards: pending ? [] : cards,
        calibration: { done: 15, target: 15 },
      });
    }
    if (pathname === "/api/swipe/vote" && request.method() === "POST") {
      calls.votes.push(request.postDataJSON());
      return json({ status: "success", vote: {}, profile_refresh: false });
    }
    if (pathname === "/api/swipe/request" && request.method() === "POST") {
      calls.requests.push(request.postDataJSON());
      return json({ status: "success", request_status: "awaiting_approval" });
    }
    if (pathname === "/api/swipe/profile") {
      return json({ status: "success", profile: null, refreshing: false, refresh_error: null });
    }
    if (pathname === "/api/swipe/stats") return json({ status: "success", stats: { total: 0 } });
    return json({});
  });
  return calls;
}

async function openSwipe(page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("e2e-password");
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.getByRole("button", { name: /Swipe/ }).click();
}

test("swipe: cards arrive after a pending batch, like asks to request, seen-it takes one click", async ({ page }) => {
  const calls = await mockApi(page);
  await openSwipe(page);

  await expect(page.getByRole("heading", { name: /Dune/ })).toBeVisible();
  expect(calls.batch).toBeGreaterThanOrEqual(2);
  expect(calls.lastNovelty).toBe("balanced");
  await expect(page.getByRole("button", { name: "Watch the trailer" })).toBeVisible();

  await page.getByRole("button", { name: "Like", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Request this movie?" })).toBeVisible();
  await page.getByRole("button", { name: "Request", exact: true }).click();
  await expect.poll(() => calls.requests.length).toBe(1);
  expect(calls.requests[0].card.id).toBe(438631);
  await expect(page.getByRole("heading", { name: "Request this movie?" })).toBeHidden();

  await expect(page.getByRole("heading", { name: /Dark/ })).toBeVisible();
  await expect(page.getByText("Wildcard", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Seen, liked/ }).click();
  await expect(page.getByRole("heading", { name: /Game of Thrones/ })).toBeVisible();
  await expect(page.locator(".modal-overlay")).toHaveCount(0);

  await page.keyboard.press("ArrowLeft");
  await expect.poll(() => calls.votes.map((v) => v.vote)).toEqual(["like", "seen_liked", "dislike"]);
  expect(calls.votes.map((v) => v.card.id)).toEqual([438631, 70523, 1399]);
});

test("swipe without an AI provider explains the setup and never asks for cards", async ({ page }) => {
  const calls = await mockApi(page, { llmConfigured: false });
  await openSwipe(page);

  await expect(page.getByRole("heading", { name: "Swipe needs an AI provider" })).toBeVisible();
  await page.waitForTimeout(500);
  expect(calls.batch).toBe(0);
});

test("swipe can request liked cards right away", async ({ page }) => {
  const calls = await mockApi(page);
  await page.addInitScript(() => localStorage.setItem("suggestarr_swipe_auto_request", "1"));
  await openSwipe(page);

  await expect(page.getByRole("heading", { name: /Dune/ })).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => calls.requests.length).toBe(1);
  await expect(page.getByRole("heading", { name: "Request this movie?" })).toHaveCount(0);
  await expect(page.getByText("Dune is waiting for approval in Requests.")).toBeVisible();
});
