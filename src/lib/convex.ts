/**
 * Convex client setup with a graceful in-browser mock fallback.
 *
 * In production / local dev:
 *   set VITE_CONVEX_URL=<your convex deployment URL>
 *   run `npx convex dev` in another terminal
 *
 * If VITE_CONVEX_URL is missing (e.g., Lovable preview), we expose a
 * mock client that implements the same useQuery/useMutation/useAction
 * surface used by the app, backed by an in-memory store. This is ONLY
 * for the demo shell — swap to real Convex by adding the env var.
 */
import { ConvexReactClient } from "convex/react";

export const HAS_REAL_CONVEX = Boolean(import.meta.env.VITE_CONVEX_URL);

export const convex = HAS_REAL_CONVEX
  ? new ConvexReactClient(import.meta.env.VITE_CONVEX_URL as string)
  : null;

export const ENABLE_ORG_CHART =
  String(import.meta.env.VITE_ENABLE_ORG_CHART ?? "true").toLowerCase() === "true";
