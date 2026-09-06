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
import { getTimeFormatter } from '../../../src/time-format';
import stringifyTimeInput from '../../../src/time-format/utils/stringifyTimeInput';

const iso = (d: Date) => d.toISOString();

test('stringifyTimeInput treats a 4-digit string as a year', () => {
  expect(stringifyTimeInput('2017', iso)).toEqual('2017-01-01T00:00:00.000Z');
  expect(getTimeFormatter('%Y')('2017')).toEqual('2017');
});

test('stringifyTimeInput treats an 8-digit string as YYYYMMDD', () => {
  expect(stringifyTimeInput('20260903', iso)).toEqual(
    '2026-09-03T00:00:00.000Z',
  );
  expect(getTimeFormatter('%Y-%m-%d')('20260903')).toEqual('2026-09-03');
});

test('stringifyTimeInput treats long digit strings as epoch milliseconds', () => {
  expect(stringifyTimeInput('1487071353000', iso)).toEqual(
    '2017-02-14T11:22:33.000Z',
  );
  expect(stringifyTimeInput(' 1487071353000 ', iso)).toEqual(
    '2017-02-14T11:22:33.000Z',
  );
  expect(stringifyTimeInput('-86400000', iso)).toEqual(
    '1969-12-31T00:00:00.000Z',
  );
  expect(getTimeFormatter('%Y')('1487071353000')).toEqual('2017');
});

test('stringifyTimeInput keeps short epoch strings as epoch milliseconds', () => {
  expect(stringifyTimeInput('0', iso)).toEqual('1970-01-01T00:00:00.000Z');
  expect(stringifyTimeInput('86400000', iso)).toEqual(
    '1970-01-02T00:00:00.000Z',
  );
  expect(stringifyTimeInput('12345678901', iso)).toEqual(
    '1970-05-23T21:21:18.901Z',
  );
});

test('stringifyTimeInput does not normalize invalid or low-year calendar keys', () => {
  expect(stringifyTimeInput('0001', iso)).toEqual('0001-01-01T00:00:00.000Z');
  expect(stringifyTimeInput('20230229', iso)).toEqual(
    '1970-01-01T05:37:10.229Z',
  );
  expect(stringifyTimeInput('20231301', iso)).toEqual(
    '1970-01-01T05:37:11.301Z',
  );
});

test('stringifyTimeInput still handles non-string and null inputs', () => {
  expect(stringifyTimeInput(1487071353000, iso)).toEqual(
    '2017-02-14T11:22:33.000Z',
  );
  expect(stringifyTimeInput(new Date(Date.UTC(2020, 0, 1)), iso)).toEqual(
    '2020-01-01T00:00:00.000Z',
  );
  expect(stringifyTimeInput(null, iso)).toEqual('null');
  expect(stringifyTimeInput(undefined, iso)).toEqual('undefined');
});
