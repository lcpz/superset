/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

/**
 * Build a UTC `Date` from explicit calendar components.
 *
 * Uses `setUTCFullYear` (rather than the `Date.UTC`/constructor path) so that
 * years 0-99 are interpreted literally instead of being offset into the 1900s
 * (e.g. year `1` stays year 1, not 1901).
 *
 * @param year - Full UTC year (no 1900 offset applied).
 * @param month - Zero-based month (0 = January).
 * @param day - Day of the month.
 * @returns A `Date` at UTC midnight for the given components.
 */
function utcDate(year: number, month: number, day: number): Date {
  const date = new Date(0);
  date.setUTCFullYear(year, month, day);
  return date;
}

/**
 * Interpret a digits-only string as either a calendar key or epoch milliseconds.
 *
 * Classification by shape:
 * - exactly 4 digits -> `YYYY` (UTC Jan 1 of that year).
 * - exactly 8 digits that round-trip as `YYYYMMDD` -> that UTC date. An 8-digit
 *   value whose components do not round-trip (e.g. `20230229`) is not a valid
 *   date and falls through to the epoch-milliseconds branch.
 * - anything else (incl. negative values and other lengths) -> epoch
 *   milliseconds via `new Date(Number(trimmed))`.
 *
 * @param trimmed - A whitespace-trimmed string matching `/^-?\d+$/`.
 * @returns The parsed `Date`.
 */
function parseDigitsOnlyString(trimmed: string): Date {
  if (trimmed.length === 4) {
    return utcDate(Number(trimmed), 0, 1);
  }
  if (trimmed.length === 8) {
    const year = Number(trimmed.slice(0, 4));
    const month = Number(trimmed.slice(4, 6)) - 1;
    const day = Number(trimmed.slice(6, 8));
    const date = utcDate(year, month, day);
    if (
      date.getUTCFullYear() === year &&
      date.getUTCMonth() === month &&
      date.getUTCDate() === day
    ) {
      return date;
    }
  }
  return new Date(Number(trimmed));
}

export default function stringifyTimeInput(
  value: Date | number | string | undefined | null,
  fn: (time: Date) => string,
) {
  if (value === null || value === undefined) {
    return `${value}`;
  }

  if (typeof value === 'string') {
    const trimmed = value.trim();
    const isIntegerString = /^-?\d+$/.test(trimmed);
    return fn(
      isIntegerString ? parseDigitsOnlyString(trimmed) : new Date(value),
    );
  }

  return fn(value instanceof Date ? value : new Date(value));
}
