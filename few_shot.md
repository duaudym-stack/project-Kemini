# Few-shot Examples

> 운영 전 1회 합성 후, 휴먼 검수를 거쳐 본 파일에 추가합니다.
> 이 예시들은 master.md 프롬프트에 in-context 예시로 삽입됩니다.

## Example 1
Input: "□ (정의) 바이오차는 농업 부산물을 고온 열분해하여 생산되는 탄소 소재로, 토양 개량 및 탄소 저장 효과가 있다. 바이오차의 열분해 과정은 300~700°C에서 진행되며, 원료에 따라 물성이 달라진다."
Output:
```json
{
  "figures": [
    {
      "figure_id": "F1",
      "section_path": "□ (정의) 바이오차",
      "anchor_text": "(정의) 바이오차는 농업 부산물을 고온 열분해하여 생산되는 탄소 소재로",
      "position_hint": "after",
      "figure_type": "concept_diagram",
      "trigger_scores": {"T1":0.8,"T2":0.1,"T3":0.0,"T4":0.3,"T5":0.2,"T6":0.0},
      "cognitive_load": 0.7,
      "final_score": 0.74,
      "purpose": "바이오차의 열분해 과정과 생산 원리를 시각적으로 설명",
      "suggested_content": ["농업 부산물","열분해","300~700°C","탄소 소재","토양 개량","탄소 저장"],
      "image_prompt_seed": "isometric concept diagram of biochar pyrolysis process from agricultural waste to carbon material",
      "confidence": 0.85
    }
  ]
}
```

## Example 2
Input: "1. 사업 추진 경위\n가. 2023년 기본계획 수립\n나. 2024년 실시설계 완료\n다. 2025년 1단계 착공\n라. 2026년 2단계 착공 예정\n마. 2028년 준공 목표"
Output:
```json
{
  "figures": [
    {
      "figure_id": "F1",
      "section_path": "1. 사업 추진 경위",
      "anchor_text": "2023년 기본계획 수립 … 2028년 준공 목표",
      "position_hint": "after",
      "figure_type": "roadmap",
      "trigger_scores": {"T1":0.1,"T2":0.1,"T3":0.0,"T4":0.9,"T5":0.2,"T6":0.0},
      "cognitive_load": 0.8,
      "final_score": 0.84,
      "purpose": "2023~2028년 사업 추진 일정을 타임라인으로 시각화",
      "suggested_content": ["2023년","기본계획","2024년","실시설계","2025년","1단계 착공","2026년","2단계 착공","2028년","준공"],
      "image_prompt_seed": "horizontal timeline roadmap from 2023 to 2028 showing construction project milestones",
      "confidence": 0.90
    }
  ]
}
```

> **추가 예시 필요**: 도메인별(정책/R&D/시설/IT/환경) 5~8편을 합성 후 보정하여 추가하세요.
