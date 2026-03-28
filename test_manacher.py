def longest_palindrome(s: str) -> str:
    """Find the longest palindromic substring using Manacher's algorithm."""
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


# Test cases
tests = [
    ("babad", "bab"),
    ("cbbd", "bb"),
    ("a", "a"),
    ("racecar", "racecar"),
    ("abacdfgdcaba", "aba"),
    ("noon", "noon"),
    ("aacecaaa", "aacecaa"),
    ("aaaa", "aaaa"),
    ("abcde", "a"),
    ("", ""),
    ("abcdefedcb", "fed"),
]

print("Test Results:")
print("-" * 40)
for s, expected in tests:
    result = longest_palindrome(s)
    is_pal = result == result[::-1]
    status = "✓" if is_pal else "✗"
    print(f"{status} s='{s}' -> '{result}' (len={len(result)}) | is_palindrome={is_pal}")
    if not is_pal:
        print(f"   ERROR: '{result}' is not a palindrome!")

print("-" * 40)

# Verify all results are valid palindromes
all_passed = all(longest_palindrome(t) == longest_palindrome(t)[::-1] for t in tests)
print(f"\nAll tests passed: {all_passed}")
