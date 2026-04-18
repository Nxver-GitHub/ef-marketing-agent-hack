import { createClient } from "@supabase/supabase-js";
import type { Database } from "./database.types";

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string;

export const HAS_REAL_SUPABASE = Boolean(supabaseUrl && supabaseAnonKey);

export const supabase = HAS_REAL_SUPABASE
  ? createClient<Database>(supabaseUrl, supabaseAnonKey)
  : null;

export const ENABLE_ORG_CHART =
  String(import.meta.env.VITE_ENABLE_ORG_CHART ?? "true").toLowerCase() === "true";
