---
title: Longhorn 볼륨이 "attaching" 상태에서 멈춤
tags: [longhorn, storage, csi, node-reboot]
---

# Longhorn 볼륨이 "attaching" 상태에서 멈춤

## 증상
파드가 `ContainerCreating` 상태에서 멈춰 있고, 이벤트에 `FailedAttachVolume` 또는
`Multi-Attach error for volume`이 표시됩니다. Longhorn UI에서는 볼륨이 `attaching` 또는
`faulted` 상태로 보입니다.

## 우선 확인 사항 (읽기 전용)
1. 파드 이벤트에서 워크로드의 네임스페이스와 PVC/PV 이름을 확인합니다.
2. 애플리케이션을 의심하기 전에 `longhorn-system`부터 확인합니다. `longhorn-manager` 파드 목록과
   볼륨 상태를 조회하세요. degraded/faulted 상태의 볼륨은 애플리케이션 문제가 아니라 Longhorn
   문제입니다.
3. 노드 상태를 확인합니다 — 최근에 재부팅되었거나 `NotReady`인 노드가 있으면, 흔히 볼륨이 죽은
   노드에 연결된 채로 남아 있습니다.

## 흔한 근본 원인
- **노드 재부팅 후 남은 오래된 연결(stale attachment).** 볼륨이 사라진 노드에 여전히 연결된
  것으로 기록되어 있습니다. 보통 Longhorn이 몇 분 안에 상태를 맞추지만(reconcile), 그렇지 않으면
  `volumeattachment`가 멈춘 상태입니다.
- **레플리카 스케줄링 실패.** 레플리카 수를 충족할 만큼 정상적인 노드/디스크가 부족합니다. 볼륨이
  `degraded` 상태로 남고 연결을 거부할 수 있습니다.
- 대상 노드의 **instance-manager 파드가 크래시 루프(crashloop)** 상태입니다.

## 에스컬레이션
볼륨이 `faulted` 상태라면 아무것도 삭제하지 마세요 — Longhorn 지원 번들(support bundle)을 수집해
스토리지 담당자에게 에스컬레이션하세요. 삭제하면 데이터가 손실될 위험이 있습니다.
