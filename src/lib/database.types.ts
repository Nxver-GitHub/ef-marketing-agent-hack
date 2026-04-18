/**
 * Supabase database types — regenerate with:
 *   npx supabase gen types typescript --project-id <project-id> > src/lib/database.types.ts
 *
 * Placeholder until project credentials are connected.
 */
export type Json = string | number | boolean | null | { [key: string]: Json } | Json[];

export interface Database {
  public: {
    Tables: {
      prospects: {
        Row: {
          id: string;
          name: string;
          company: string;
          role: string;
          industry: string;
          linkedin_url: string | null;
          created_at: string;
          updated_at: string;
        };
        Insert: Omit<Database["public"]["Tables"]["prospects"]["Row"], "id" | "created_at" | "updated_at"> & {
          id?: string;
          created_at?: string;
          updated_at?: string;
        };
        Update: Partial<Database["public"]["Tables"]["prospects"]["Insert"]>;
      };
      signals: {
        Row: {
          id: string;
          prospect_id: string;
          source: string;
          signal_type: string;
          value: Json;
          raw_data: Json | null;
          weight: number;
          confidence: number;
          collected_at: string;
        };
        Insert: Omit<Database["public"]["Tables"]["signals"]["Row"], "id" | "collected_at"> & {
          id?: string;
          collected_at?: string;
        };
        Update: Partial<Database["public"]["Tables"]["signals"]["Insert"]>;
      };
      scores: {
        Row: {
          id: string;
          prospect_id: string;
          authenticity_score: number;
          authority_score: number;
          warmth_score: number;
          overall_score: number;
          falsification_notes: string[];
          computed_at: string;
        };
        Insert: Omit<Database["public"]["Tables"]["scores"]["Row"], "id" | "computed_at"> & {
          id?: string;
          computed_at?: string;
        };
        Update: Partial<Database["public"]["Tables"]["scores"]["Insert"]>;
      };
      signal_weights: {
        Row: {
          id: string;
          signal_type: string;
          authenticity_weight: number;
          authority_weight: number;
          warmth_weight: number;
        };
        Insert: Omit<Database["public"]["Tables"]["signal_weights"]["Row"], "id"> & { id?: string };
        Update: Partial<Database["public"]["Tables"]["signal_weights"]["Insert"]>;
      };
      scoring_runs: {
        Row: {
          id: string;
          prospect_id: string;
          status: "pending" | "running" | "complete" | "error";
          sources_attempted: string[];
          sources_succeeded: string[];
          current_source: string | null;
          error_log: string | null;
          started_at: string;
          completed_at: string | null;
        };
        Insert: Omit<Database["public"]["Tables"]["scoring_runs"]["Row"], "id" | "started_at"> & {
          id?: string;
          started_at?: string;
        };
        Update: Partial<Database["public"]["Tables"]["scoring_runs"]["Insert"]>;
      };
    };
  };
}
