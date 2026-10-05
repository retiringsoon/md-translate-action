# Prompt Guide

The **pinning** grinder recovers the public key from a signature. A small dataset
makes overfitting easy, so keep the validation split honest.

```python
def grind(seed: int) -> int:
    return seed * 2  # must survive translation byte-for-byte
```

| setting | value |
|---|---|
| leading zeros | 24 |
| window | 1200s |
