import { deleteJson, fetchJson, patchJson, postJson, putJson } from "./http"
import type { LearnedRuleModel, OrgLearnedRuleModel, RuleModel } from "./types"

const repoQuery = (owner: string, repo: string) =>
  `?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`

// Custom rules (global + per-repo) and learned rules.
export const rulesApi = {
  // Learned rules. status: "approved" | "pending" | "rejected" | "" (all)
  listLearnedRules: (status = "") =>
    fetchJson<OrgLearnedRuleModel[]>(
      status
        ? `/api/learned-rules?status=${encodeURIComponent(status)}`
        : `/api/learned-rules`
    ),

  listRepoLearnedRules: (owner: string, repo: string) =>
    fetchJson<LearnedRuleModel[]>(`/api/repos/${owner}/${repo}/learned-rules`),

  getLearnedRule: (owner: string, repo: string, id: number) =>
    fetchJson<OrgLearnedRuleModel>(
      `/api/learned-rules/${id}${repoQuery(owner, repo)}`
    ),

  approveLearnedRule: (owner: string, repo: string, id: number) =>
    postJson<{ ok: boolean }>(
      `/api/learned-rules/${id}/approve${repoQuery(owner, repo)}`,
      {}
    ),

  rejectLearnedRule: (owner: string, repo: string, id: number) =>
    postJson<{ ok: boolean }>(
      `/api/learned-rules/${id}/reject${repoQuery(owner, repo)}`,
      {}
    ),

  setLearnedRuleActive: (
    owner: string,
    repo: string,
    id: number,
    active: boolean
  ) =>
    patchJson<{ ok: boolean }>(
      `/api/learned-rules/${id}/active${repoQuery(owner, repo)}`,
      { active }
    ),

  createLearnedRule: (
    owner: string,
    repo: string,
    body: { rule_text: string; category: string; path_pattern?: string }
  ) =>
    postJson<LearnedRuleModel>(
      `/api/learned-rules${repoQuery(owner, repo)}`,
      body
    ),

  updateLearnedRule: (
    owner: string,
    repo: string,
    id: number,
    body: { rule_text: string; category: string; path_pattern?: string }
  ) =>
    putJson<{ ok: boolean }>(
      `/api/learned-rules/${id}${repoQuery(owner, repo)}`,
      body
    ),

  deleteLearnedRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/learned-rules/${id}${repoQuery(owner, repo)}`),

  // Global rules
  listGlobalRules: () => fetchJson<RuleModel[]>("/api/rules/global"),

  createGlobalRule: (title: string, content: string) =>
    postJson<RuleModel>("/api/rules/global", { title, content }),

  updateGlobalRule: (id: number, title: string, content: string) =>
    putJson<RuleModel>(`/api/rules/global/${id}`, { title, content }),

  deleteGlobalRule: (id: number) => deleteJson(`/api/rules/global/${id}`),

  toggleGlobalRule: (id: number) =>
    patchJson<RuleModel>(`/api/rules/global/${id}/toggle`),

  // Per-repo rules
  listRepoRules: (owner: string, repo: string) =>
    fetchJson<RuleModel[]>(`/api/repos/${owner}/${repo}/rules`),

  createRepoRule: (
    owner: string,
    repo: string,
    title: string,
    content: string
  ) =>
    postJson<RuleModel>(`/api/repos/${owner}/${repo}/rules`, {
      title,
      content,
    }),

  updateRepoRule: (
    owner: string,
    repo: string,
    id: number,
    title: string,
    content: string
  ) =>
    putJson<RuleModel>(`/api/repos/${owner}/${repo}/rules/${id}`, {
      title,
      content,
    }),

  deleteRepoRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/rules/${id}`),
}
