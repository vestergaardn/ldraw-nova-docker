import { test } from 'node:test';
import assert from 'node:assert/strict';
import { budgetChoice, parseBudget, needsBudget, MAX_BUDGET_CENTS } from '../src/budget.ts';

test('decimal input becomes exact cents without rounding extra digits', () => {
  for (const [value, expected] of [['100', 10000], ['19.99',1999], ['.50',50], ['0.01',1], [' 12.30 ',1230]]) {
    assert.equal(parseBudget(value), expected);
  }
  for (const value of ['', '0', '-1', '1.234', '1e2', 'NaN', 'Infinity', '$100', '100,00', String(MAX_BUDGET_CENTS)]) {
    assert.equal(parseBudget(value), null, value);
  }
});

test('5% allowance floors to cents', () => {
  assert.equal(budgetChoice(10000).maximum_cents,10500);
  assert.equal(budgetChoice(9999).maximum_cents,10498);
  assert.equal(budgetChoice(19).maximum_cents,19);
  assert.equal(budgetChoice(20).maximum_cents,21);
});

test('prompt only precedes the first writable build', () => {
  const options={mode:'agent',permissions:'full'};
  assert.equal(needsBudget(budgetChoice(null,true),options),true);
  assert.equal(needsBudget(budgetChoice(null,true),{...options,mode:'plan'}),false);
  assert.equal(needsBudget(budgetChoice(null,true),{...options,permissions:'read_only'}),false);
  assert.equal(needsBudget(budgetChoice(null),options),false);
  assert.equal(needsBudget(budgetChoice(100),options),false);
});
