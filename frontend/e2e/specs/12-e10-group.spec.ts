// E10 (spec §18.7, §15.5, §8.7; plan M4 Task 34): the fixtures' Adzuna re-ingest pair, one duplicate group
// whose member waits on its representative. After E3's profile v2 the spec first asserts, as the setup's step 9
// checks it (`reingestPair`) and through the API, that the member's target under the new tuple is
// `deferred_duplicate` in the group again. Then the group's card: "scored via" its representative; Split of the
// member, after which the member is a card of its own, "Pending — queued", with Score now.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, apiContext } from "../support/api";
import { en, expect, filled, templateMatch, test } from "../support/fixtures";
import { cardOf, jobDetail, SCORE_NOW, searchFor } from "../support/jobs";
import { reingestPair } from "../support/stack";

// The Split is one-shot per stack: a retry could only fail on the split pair (Ruling T33-b).
test.describe.configure({ retries: 0 });

test("E10: the re-ingest pair is a group card scored via its representative, and Split makes the member its own pending card", async ({
  page,
}) => {
  const pair = await reingestPair();
  const said = `setup step 9's reingest_group under profile v2: ${String(pair.problem)}`;
  expect(pair.problem, said).toBeNull();
  const api = await apiContext(page);
  try {
    const member = await jobDetail(api, pair.member);
    expect([member.target?.state, member.group?.id], said).toEqual(["deferred_duplicate", pair.groupId]);
    const representative = await jobDetail(api, pair.representative);
    const title = representative.job.title;

    await page.goto("/");
    const listed = await searchFor(page, title);
    const groupCard = listed.items.find((card) => card.job_id === pair.representative);
    expect(groupCard?.kind, `job ${String(pair.representative)}'s card`).toBe("group");
    const size = groupCard?.group?.size ?? 0;
    const card = cardOf(page, pair.representative);
    const header = [
      filled(en["group.possible_duplicates"], { size: String(size) }),
      filled(en["group.scored_via"], { sources: en["source.adzuna"], title }),
      filled(size === 1 ? en["group.applies_one"] : en["group.applies_other"], { count: String(size) }),
    ].join(" · ");
    await expect(card.getByText(header, { exact: true })).toBeVisible();
    await expect(cardOf(page, pair.member)).toHaveCount(0);
    await expectNoSeriousA11yViolations(page);

    await card.getByRole("button", { name: templateMatch(en["group.show_members"], { size: String(size) }) }).click();
    const members = card.getByRole("list", { name: en["group.members"] });
    await expect(members.getByRole("listitem")).toHaveCount(size);
    await expectNoSeriousA11yViolations(page);
    const memberItem = members.getByRole("listitem").filter({ hasNotText: en["group.representative"] });
    await expect(memberItem).toHaveCount(1);
    const split = answerTo(page, "POST", `/api/duplicate-groups/${String(pair.groupId)}/split`);
    await memberItem.getByRole("button", { name: en["group.split"], exact: true }).click();
    const answered = await split;
    expect(answered.status(), `the split of job ${String(pair.member)} from group ${String(pair.groupId)}`).toBe(200);
    expect(answered.request().postDataJSON()).toEqual({ job_id: pair.member });

    const own = cardOf(page, pair.member);
    await expect(own).toBeVisible();
    await expect(own.getByText(en["chip.pending"], { exact: true })).toBeVisible();
    await expect(own.getByRole("button", { name: SCORE_NOW })).toBeEnabled();
    const after = await jobDetail(api, pair.member);
    expect([after.target?.state, after.group]).toEqual(["pending", null]);
    await expectNoSeriousA11yViolations(page);
  } finally {
    await api.dispose();
  }
});
