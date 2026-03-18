# DisneyPlus(디즈니플러스)

```
uv run unshackle dl -vl all -al orig -sl ko,en,ja -q 2160 -v h.265 -r HDR10 DSNP entity-4d12671a-f0ad-4c3f-8526-09ae6772390b
```

## Information

- Authorization: Credentials, Web Token
- Security: UHD@L1/SL3000, FHD@L1/SL3000, HD@L3/SL2000
- Working Client Agent: AndroidTV
- Support Codec
  - Video: H264, H265
  - Audio: AAC, AC3, ATMOS, DTS:X(P2:IMAX)
  - Range: SDR, HDR10, DV

## Support Args

- `-i`, `--imax`: Prefer IMAX Enhanced version if available.
- `-r`, `--remastered-ar`: Prefer Remastered Aspect Ratio if available.
- `-e`, `--extras`: Select a extras video if available.
- `-tu`, `--tier-unlimits`: Remove stream quality restrictions for a specific account.

## Tips

- Configure user settings within the `envied.yaml` file.
  ```
  services:
    DSNP:
      # Specifies the index of the profile to use. (0 = first profile, 1 = second profile, etc.)
      # Automatically select a profile or language when commenting.
      # Language setting is only available in languages supported by Disney+.
      preferences:
        profile: 0
        # language: "ko"
  ```
- To enable the tier_unlimits command by default, add the following to `envied.yaml`.
  ```
  dl:
    ...
    DSNP:
      tier_unlimits: True
  ```
