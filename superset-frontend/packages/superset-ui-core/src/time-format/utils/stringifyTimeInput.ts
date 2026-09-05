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

// Digits-only strings shorter than this are treated as calendar values
// (YYYY / YYYYMMDD) rather than epoch milliseconds.
const MIN_EPOCH_MS_DIGITS = 12;
const YEAR_MONTH_DAY = /^(\d{4})(\d{2})(\d{2})$/;

function parseDigitsOnlyString(trimmed: string): Date {
  if (trimmed.startsWith('-') || trimmed.length >= MIN_EPOCH_MS_DIGITS) {
    return new Date(Number(trimmed));
  }
  if (trimmed.length === 4) {
    return new Date(Date.UTC(Number(trimmed), 0, 1));
  }
  const ymd = YEAR_MONTH_DAY.exec(trimmed);
  if (ymd) {
    return new Date(
      Date.UTC(Number(ymd[1]), Number(ymd[2]) - 1, Number(ymd[3])),
    );
  }
  return new Date(trimmed);
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
