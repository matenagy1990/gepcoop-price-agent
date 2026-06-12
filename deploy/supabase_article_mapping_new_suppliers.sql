-- Add new supplier columns to article_mapping.
-- Run once in the Supabase SQL Editor.
-- Safe to run multiple times (IF NOT EXISTS).

alter table article_mapping
  add column if not exists argip_part_no text,
  add column if not exists vipa_part_no text,
  add column if not exists inoxmare_part_no text;
