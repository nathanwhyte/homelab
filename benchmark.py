import time
import random
import string


def longest_palindrome_naive(s: str) -> str:
    """Naive O(n²) expand-around-center approach."""
    if len(s) < 2:
        return s

    start, end = 0, 0

    def expand(left, right):
        while left >= 0 and right < len(s) and s[left] == s[right]:
            left -= 1
            right += 1
        return left + 1, right - 1

    for i in range(len(s)):
        l1, r1 = expand(i, i)
        l2, r2 = expand(i, i + 1)
        if r1 - l1 > end - start:
            start, end = l1, r1
        if r2 - l2 > end - start:
            start, end = l2, r2

    return s[start:end + 1]


def longest_palindrome_manacher(s: str) -> str:
    """Manacher's O(n) algorithm."""
    if len(s) < 2:
        return s

    t = '#' + '#'.join(s) + '#'
    n = len(t)
    d = [0] * n
    c, r = 0, 0

    for i in range(n):
        mirror = 2 * c - i
        d[i] = min(r - i, d[mirror]) if i < r else 0

        while i + d[i] + 1 < n and i - d[i] - 1 >= 0 and t[i + d[i] + 1] == t[i - d[i] - 1]:
            d[i] += 1

        if i + d[i] > r:
            c, r = i, i + d[i]

    max_len, max_center_idx = max((d[i], i) for i in range(n))
    start = (max_center_idx - max_len) // 2
    return s[start:start + max_len]


def time_algorithm(func, s, iterations=10):
    """Time a function on string s."""
    start = time.perf_counter()
    for _ in range(iterations):
        func(s)
    end = time.perf_counter()
    return (end - start) / iterations


# Generate test strings
print("Benchmark Results")
print("=" * 60)

for size in [100, 1000, 5000, 10000]:
    # Random string with some repeating patterns
    s = ''.join(random.choices('abcdefghij', k=size))

    naive_time = time_algorithm(longest_palindrome_naive, s)
    manacher_time = time_algorithm(longest_palindrome_manacher, s)

    speedup = naive_time / manacher_time if manacher_time > 0 else float('inf')

    print(f"Size {size:5d} chars: Naive={naive_time:.6f}s | Manacher={manacher_time:.6f}s | Speedup={speedup:.1f}x")

# Test correctness on a few cases
print("\nCorrectness verification:")
test_strings = ["abcde", "racecar", "abacdfgdcaba", "a" * 1000 + "racecar" + "a" * 1000]
for s in test_strings:
    naive_result = longest_palindrome_naive(s)
    manacher_result = longest_palindrome_manacher(s)
    match = "✓" if naive_result == manacher_result == naive_result[::-1] else "✗"
    print(f"  {match} {s[:50]}{'...' if len(s) > 50 else ''} -> '{naive_result}'")
